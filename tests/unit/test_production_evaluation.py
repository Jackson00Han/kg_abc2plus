"""Hand-computable Stage 9 production-reference report tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import unittest

from graphrag_prod.evaluation.production import (
    PRODUCTION_OBSERVATION_SCHEMA_VERSION,
    REQUIRED_DEPLOYMENT_PREREQUISITES,
    _EXPECTED_ACCESS_INVENTORY,
    _EXPECTED_ACCESS_PRINCIPAL,
    _EXPECTED_ACCESS_PROBES,
    build_production_candidate_report,
)
from graphrag_prod.evaluation.production_config import (
    PRODUCTION_ANSWER_RETRIEVAL_LIMITS,
)
from graphrag_prod.evaluation.quality_evidence import (
    QUALITY_CASE_EVIDENCE_SCHEMA_VERSION,
    build_quality_case_evidence,
    canonical_quality_digest,
    evaluate_quality_case_evidence,
)
from graphrag_prod.evaluation.reference_predictions import (
    REFERENCE_PREDICTION_PROVIDER,
    REFERENCE_PREDICTION_SHA256,
    REFERENCE_PREDICTION_VERSION,
)
from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION


ROOT = Path(__file__).parents[2]

LIFECYCLE_SCENARIOS = (
    "idempotency",
    "interrupted_ingestion",
    "deletion",
    "access_isolation",
    "backup_restore",
)
DEPENDENCIES = ("neo4j", "embedding_provider", "llm")
DEPENDENCY_MODES = ("success", "timeout", "unavailable", "failure")
PROVIDER_TIMEOUT_SCENARIOS = {"embedding_provider_timeout", "llm_timeout"}
SUITES = ("functional", "performance", "quality", "recovery", "security")
EVIDENCE_SCHEMAS = {
    "acceptance_contract": "acceptance-contract-v1",
    "answer_embedding_corpus": "dev-corpus-manifest-v1",
    "backup_dump": "neo4j-database-dump-v1",
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
    "reference_answer_predictions": "reference-answer-predictions-v1",
    "request_samples": "production-request-samples-v1",
    "retrieval_stage_samples": "production-retrieval-stage-samples-v1",
    "stage8_report": "evaluation-report-v1",
    "suite_results": "production-suite-results-v1",
    "validation_profile": "validation-profile-v1",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _prefixed(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _normalized_deletion(value: dict) -> dict:
    normalized = deepcopy(value)
    normalized["durable_audit_job_ids"] = sorted(
        normalized["durable_audit_job_ids"]
    )
    normalized["durable_audit_jobs"] = sorted(
        normalized["durable_audit_jobs"], key=lambda item: item["job_id"]
    )
    normalized["preserved_tenant_ids"] = sorted(
        normalized["preserved_tenant_ids"]
    )
    normalized["target_active_chunk_ids"] = sorted(
        normalized["target_active_chunk_ids"]
    )
    return normalized


@lru_cache(maxsize=1)
def _quality_fixture() -> tuple[dict, dict, dict]:
    questions = [
        json.loads(line)
        for line in (ROOT / "evaluation" / "gold-v1" / "questions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    answers = [
        json.loads(line)
        for line in (ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    actual_retrieval: list[dict] = []
    actual_answers: list[dict] = []
    for question, gold in zip(questions, answers):
        assert question["id"] == gold["id"]
        ranking = sorted(
            question["relevance"],
            key=lambda chunk_id: (-float(question["relevance"][chunk_id]), chunk_id),
        )
        actual_retrieval.append(
            {"id": question["id"], "ranking": ranking, "visible_resources": []}
        )
        if gold["expected_status"] == "insufficient_context":
            actual_answers.append(
                {
                    "answer": (
                        "I don't have enough cited context to answer this question."
                    ),
                    "citations": [],
                    "claims": [],
                    "conflicts": [],
                    "failure_code": None,
                    "id": gold["id"],
                    "output_schema_version": OUTPUT_SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "status": "insufficient_context",
                }
            )
            continue
        evidence = {item["chunk_id"]: item for item in gold["evidence"]}
        chunk_ids = list(evidence)
        labels = {
            chunk_id: f"S{index}"
            for index, chunk_id in enumerate(chunk_ids, start=1)
        }
        claims = [
            {
                "citation_ids": [
                    labels[chunk_id] for chunk_id in claim["evidence_chunk_ids"]
                ],
                "inference": claim["inference"],
                "material": True,
                "text": claim["reference_text"],
            }
            for claim in gold["claims"]
        ]
        citations = [
            {
                "citation_id": labels[chunk_id],
                **{
                    key: value
                    for key, value in evidence[chunk_id].items()
                    if key != "chunk_key"
                },
            }
            for chunk_id in chunk_ids
        ]
        rendered = "\n".join(
            f"{'Inference: ' if claim['inference'] else ''}{claim['text']} "
            + " ".join(
                f"[{citation_id}]" for citation_id in claim["citation_ids"]
            )
            for claim in claims
        )
        actual_answers.append(
            {
                "answer": rendered,
                "citations": citations,
                "claims": claims,
                "conflicts": [],
                "failure_code": None,
                "id": gold["id"],
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "status": "answered",
            }
        )
    cases = build_quality_case_evidence(actual_retrieval, actual_answers)
    _, retrieval_metrics, answer_metrics = evaluate_quality_case_evidence(cases)
    return cases, retrieval_metrics, answer_metrics


def _rebind_raw(observations: dict) -> None:
    request_samples = {
        section: sorted(
            (
                {
                    **item,
                    "query_vector_checksum": _prefixed(
                        item["query_vector_checksum"]
                    ),
                }
                for item in items
            ),
            key=lambda item: item["request_id"],
        )
        for section, items in observations["request_samples"].items()
    }
    graph = observations["canonical_graph"]
    graph_states = {
        field: {
            **deepcopy(graph[field]),
            "label_counts": dict(sorted(graph[field]["label_counts"].items())),
            "sha256": _prefixed(graph[field]["sha256"]),
        }
        for field in (
            "backup_source_state",
            "post_validation_state",
            "pre_validation_state",
            "restored_state",
        )
    }
    environment = {
        **observations["runtime_environment"],
        "actual_neo4j_image_id": _prefixed(
            observations["runtime_environment"]["actual_neo4j_image_id"]
        ),
        "actual_neo4j_repo_digest": _prefixed(
            observations["runtime_environment"]["actual_neo4j_repo_digest"]
        ),
    }
    quality = deepcopy(observations["quality_evidence"])
    quality["case_set_sha256"] = _prefixed(quality["case_set_sha256"])
    quality["gold_projection_sha256"] = _prefixed(
        quality["gold_projection_sha256"]
    )
    quality["graph_state_sha256"] = _prefixed(quality["graph_state_sha256"])
    costs = observations["cost"]["request_cost_usd"]
    total_cost = math.fsum(sorted(float(item) for item in costs))
    normalized_cost = {
        "currency": "USD",
        "estimated_total_usd": total_cost,
        "input_tokens": observations["cost"]["input_tokens"],
        "mean_request_usd": total_cost / len(costs),
        "metered_requests": len(costs),
        "model_calls": observations["cost"]["model_calls"],
        "output_tokens": observations["cost"]["output_tokens"],
        "request_cost_sample_count": len(costs),
    }
    backup = deepcopy(observations["backup_evidence"])
    for field in (
        "backup_sha256",
        "restored_container_resource_sha256",
        "restored_state_sha256",
        "source_container_resource_sha256",
        "source_state_sha256",
    ):
        backup[field] = _prefixed(backup[field])
    raw_artifacts = {
        "backup_observation": backup,
        "container_inspection": environment,
        "deletion_observation": _normalized_deletion(
            observations["deletion_evidence"]
        ),
        "fault_timeline": observations["fault_timeline"],
        "graph_post_state": graph_states["post_validation_state"],
        "graph_backup_source_state": graph_states["backup_source_state"],
        "graph_pre_state": graph_states["pre_validation_state"],
        "graph_restore_state": graph_states["restored_state"],
        "large_database_quality": quality,
        "large_database_quality_cases": quality["case_evidence"],
        "ingestion_observation": observations["ingestion_evidence"],
        "load_graph_state": observations["load_graph_state"],
        "provider_usage": {
            "cost": normalized_cost,
            "embedding_latency_ms": sorted(
                float(item)
                for item in observations["latency_ms"]["embedding_provider"]
            ),
            "llm_latency_ms": sorted(
                float(item) for item in observations["latency_ms"]["llm"]
            ),
            "provider_evidence": observations["provider_evidence"],
        },
        "request_samples": request_samples,
        "retrieval_stage_samples": sorted(
            (
                {
                    "request_id": sample["request_id"],
                    "retrieval_stage_ms": sample["retrieval_stage_ms"],
                    "trace_id": sample["trace_id"],
                }
                for samples in request_samples.values()
                for sample in samples
            ),
            key=lambda item: item["request_id"],
        ),
        "suite_results": observations["suite_results"],
    }
    for evidence_id, artifact in raw_artifacts.items():
        observations["evidence_manifest"][evidence_id]["sha256"] = _sha256(artifact)


def _scenario_outcome(scenario_id: str) -> dict:
    if scenario_id == "llm_failure":
        return {
            "http_status": 200,
            "error_code": None,
            "domain_status": "refused",
            "reason": "invalid_model_output",
        }
    if scenario_id == "llm_success":
        return {
            "http_status": 200,
            "error_code": None,
            "domain_status": "answered",
            "reason": None,
        }
    if scenario_id == "neo4j_success":
        return {
            "http_status": 200,
            "error_code": None,
            "domain_status": "retrieved",
            "reason": None,
        }
    if scenario_id == "neo4j_failure":
        return {
            "http_status": 500,
            "error_code": "internal_error",
            "domain_status": None,
            "reason": None,
        }
    if scenario_id in LIFECYCLE_SCENARIOS or scenario_id.endswith("_success"):
        return {
            "http_status": 200,
            "error_code": None,
            "domain_status": None,
            "reason": None,
        }
    if scenario_id.endswith("_timeout"):
        return {
            "http_status": 504,
            "error_code": "dependency_timeout",
            "domain_status": None,
            "reason": None,
        }
    return {
        "http_status": 503,
        "error_code": "dependency_unavailable",
        "domain_status": None,
        "reason": None,
    }


def _request_sample(
    request_id: str,
    client_id: str,
    started_ms: float,
    latency_ms: float,
    status_code: int = 200,
    *,
    case_id: str,
    embedding_space_id: str,
    query_vector_checksum: str,
    expected_chunk_ids: list[str] | None = None,
    kind: str = "retrieval",
    answer_evidence: dict | None = None,
) -> dict:
    dataset_id = "gold-v1" if kind == "answer" else "load-v1"
    expected = expected_chunk_ids or ["expected-chunk"]
    return {
        "answer_evidence": answer_evidence,
        "case_id": case_id,
        "request_id": request_id,
        "retrieval_stage_ms": 1.0,
        "client_id": client_id,
        "started_monotonic_ms": started_ms,
        "completed_monotonic_ms": started_ms + latency_ms,
        "domain_failure_code": None,
        "domain_status": "answered" if kind == "answer" else None,
        "dataset_id": dataset_id,
        "embedding_space_id": embedding_space_id,
        "error_code": None,
        "expected_chunk_ids": expected,
        "inactive_chunk_ids": [],
        "inactive_version_count": 0,
        "query_vector_checksum": query_vector_checksum,
        "selected_chunk_count": 1,
        "selected_chunk_ids": [expected[0]],
        "semantic_success": True,
        "status_code": status_code,
        "trace_id": f"trace-{request_id}",
        "unauthorized_chunk_ids": [],
        "unauthorized_chunk_count": 0,
        "visible_chunk_ids": [expected[0]],
    }


def _retrieval_samples() -> list[dict]:
    # Nearest-rank p50=100, p95=900, p99=1200 over exactly 2,400 requests.
    latencies = [100.0] * 1_200 + [900.0] * 1_080 + [1_200.0] * 120
    load_manifest = _load(ROOT / "datasets" / "load-v1" / "manifest.json")
    load_queries = load_manifest["retrieval_workload"]["queries"]
    samples: list[dict] = []
    request_index = 0
    for client_index in range(8):
        client_latencies = latencies[client_index::8]
        idle_ms = (300_000.0 - sum(client_latencies)) / (
            len(client_latencies) - 1
        )
        started = 200_000.0
        for client_request_index, latency in enumerate(client_latencies):
            if client_request_index == len(client_latencies) - 1:
                started = 500_000.0 - latency
            query = load_queries[(client_index + client_request_index) % 64]
            samples.append(
                _request_sample(
                    f"retrieval-{request_index:04d}",
                    f"retrieval-{client_index:02d}",
                    started,
                    latency,
                    case_id=query["case_id"],
                    embedding_space_id=query["embedding_space_id"],
                    query_vector_checksum=query["query_vector_checksum"],
                    expected_chunk_ids=query["expected_chunk_ids"],
                )
            )
            request_index += 1
            if client_request_index < len(client_latencies) - 1:
                started += latency + idle_ms
    return samples


def _answer_samples() -> list[dict]:
    # Nearest-rank p50=1000, p95=14000, p99=16000 over exactly 30 requests.
    latencies = [1_000.0] * 15 + [14_000.0] * 14 + [16_000.0]
    config = _load(ROOT / "evaluation" / "production-reference-config.v1.json")
    case_ids = config["answer"]["gold_case_ids"]
    questions = {
        item["id"]: item
        for item in map(
            json.loads,
            (ROOT / "evaluation" / "gold-v1" / "questions.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
    }
    manifest = _load(ROOT / "datasets" / "dev-corpus-v1" / "manifest.json")
    vector_checksums = {
        item["id"]: _sha256(list(item["vector"]))
        for item in map(
            json.loads,
            (ROOT / "datasets" / "dev-corpus-v1" / "vectors.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
        if item["kind"] == "query"
    }
    quality_cases = {
        item["id"]: item["answer"] for item in _quality_fixture()[0]["cases"]
    }
    gold_answers = {
        item["id"]: item
        for item in map(
            json.loads,
            (ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
    }

    def commitment(case_id: str) -> dict:
        quality = quality_cases[case_id]
        versions = {
            item["chunk_id"]: item["version_id"]
            for item in gold_answers[case_id]["evidence"]
        }
        return {
            **quality,
            "citations": [
                {**item, "version_id": versions[item["chunk_id"]]}
                for item in quality["citations"]
            ],
            "claims": [
                {key: value for key, value in item.items() if key != "claim_id"}
                for item in quality["claims"]
            ],
            "schema_version": "production-http-answer-commitment-v1",
        }

    return [
        _request_sample(
            f"answer-{index:02d}",
            "answer-client",
            1_000_000.0 + index * 20_000.0,
            latency,
            case_id=case_ids[index],
            embedding_space_id=manifest["embedding_profile"][
                "embedding_space_id"
            ],
            query_vector_checksum=vector_checksums[
                questions[case_ids[index]]["vector_id"]
            ],
            expected_chunk_ids=sorted(questions[case_ids[index]]["relevance"]),
            kind="answer",
            answer_evidence=commitment(case_ids[index]),
        )
        for index, latency in enumerate(latencies)
    ]


def _access_evidence(started_ms: float, completed_ms: float) -> dict:
    step = (completed_ms - started_ms) / (len(_EXPECTED_ACCESS_PROBES) + 1)
    probes = []
    for index, (case_id, expected) in enumerate(
        sorted(_EXPECTED_ACCESS_PROBES.items()), start=1
    ):
        trace_id = f"trace-access-{index:02d}"
        trace = {
            "bm25_recall": [],
            "candidate_vector_ranking": [],
            "decisions": [],
            "final_ranking": [],
            "graph_expansion": [],
            "seed_ranking": [],
            "selected_chunk_ids": [],
            "trace_id": trace_id,
            "vector_recall": [],
        }
        probes.append(
            {
                "canary_chunk_id": expected["canary_chunk_id"],
                "case_id": case_id,
                "citation_chunk_ids": [],
                "completed_monotonic_ms": started_ms + step * index + step / 2,
                "embedding_space_id": expected["embedding_space_id"],
                "error_code": None,
                "http_status": 200,
                "kind": expected["kind"],
                "query_text_sha256": expected["query_text_sha256"],
                "query_vector_checksum": expected["query_vector_checksum"],
                "request_id": f"access-{index:02d}",
                "response": {"chunks": [], "trace": trace},
                "result_chunk_ids": [],
                "started_monotonic_ms": started_ms + step * index,
                "target_tenant_id": expected["target_tenant_id"],
                "trace_chunk_ids": [],
                "trace_id": trace_id,
                "version_id": expected["version_id"],
            }
        )
    return {
        "dataset_id": "load-v1",
        "inventory": deepcopy(_EXPECTED_ACCESS_INVENTORY),
        "principal": deepcopy(_EXPECTED_ACCESS_PRINCIPAL),
        "probes": probes,
        "schema_version": "load-v1-access-isolation-v1",
    }


def _passing_observations() -> dict:
    production_configuration = _load(
        ROOT / "evaluation" / "production-reference-config.v1.json"
    )
    answer_preflight_case_ids = sorted(
        production_configuration["answer"]["gold_case_ids"]
    )
    scenario_ids = list(LIFECYCLE_SCENARIOS) + [
        f"{dependency}_{mode}"
        for dependency in DEPENDENCIES
        for mode in DEPENDENCY_MODES
    ]
    scenario_latencies = {
        scenario_id: (
            5_000.0
            if scenario_id in PROVIDER_TIMEOUT_SCENARIOS
            else float(index + 1)
        )
        for index, scenario_id in enumerate(scenario_ids)
    }
    scenarios = {
        scenario_id: {
            "passed": True,
            "latency_ms": scenario_latencies[scenario_id],
            **_scenario_outcome(scenario_id),
        }
        for index, scenario_id in enumerate(scenario_ids)
    }
    fault_timeline = {
        scenario_id: {
            "started_monotonic_ms": float(index * 100),
            "completed_monotonic_ms": (
                float(index * 100) + scenario_latencies[scenario_id]
            ),
            "assertion_failures": [],
            **_scenario_outcome(scenario_id),
        }
        for index, scenario_id in enumerate(scenario_ids)
    }
    fault_timeline["idempotency"].update(
        {
            "started_monotonic_ms": 100_001.0,
            "completed_monotonic_ms": 100_002.0,
        }
    )
    access_event = fault_timeline["access_isolation"]
    access_event["access_evidence"] = _access_evidence(
        access_event["started_monotonic_ms"],
        access_event["completed_monotonic_ms"],
    )
    request_samples = {
        "retrieval": _retrieval_samples(),
        "answer": _answer_samples(),
    }
    suite_results = {
        suite: {
            "tests_run": 1,
            "passed_test_ids": [f"{suite}.test_reference"],
            "failed_test_ids": [],
            "error_test_ids": [],
            "skipped_test_ids": [],
        }
        for suite in SUITES
    }
    load_graph_expectations = _load(
        ROOT / "datasets" / "load-v1" / "manifest.json"
    )["graph_expectations"]
    load_graph_shape = load_graph_expectations["after_generation_activation"]
    canonical_graph_state = {
        "business_node_count": load_graph_shape["business_node_count"] + 1_000,
        "business_relationship_count": (
            load_graph_shape["business_relationship_count"] + 2_000
        ),
        "label_counts": {
            **load_graph_shape["label_counts"],
            "Assertion": 100,
        },
        "schema_and_indexes_verified": True,
        "sha256": "c" * 64,
    }
    canonical_graph = {
        "backup_source_state": deepcopy(canonical_graph_state),
        "post_validation_state": deepcopy(canonical_graph_state),
        "pre_validation_state": deepcopy(canonical_graph_state),
        "restored_state": deepcopy(canonical_graph_state),
    }
    versions = {
        "answer_embedding_corpus_digest": _file_sha256(
            ROOT / "datasets" / "dev-corpus-v1" / "manifest.json"
        ),
        "answer_embedding_corpus_version": "1.0.1",
        "answer_embedding_provider": "fixture",
        "answer_embedding_model": "adjudicated-evidence-clusters",
        "answer_embedding_revision": "dev-corpus-v1.1",
        "answer_embedding_space_id": (
            "19ef2d72-d978-5d0d-9f75-b7f33f9b6f4d"
        ),
        "answer_prediction_digest": REFERENCE_PREDICTION_SHA256,
        "answer_prediction_provider": REFERENCE_PREDICTION_PROVIDER,
        "answer_prediction_version": REFERENCE_PREDICTION_VERSION,
        "contract_version": "1.0.1",
        "contract_digest": _file_sha256(
            ROOT / "contracts" / "acceptance.v1.json"
        ),
        "profile_version": "1.0.0",
        "profile_digest": _file_sha256(
            ROOT / "contracts" / "profiles" / "production-reference.v1.json"
        ),
        "load_corpus_id": "load-v1",
        "load_corpus_version": "1.0.2",
        "load_corpus_digest": _file_sha256(
            ROOT / "datasets" / "load-v1" / "manifest.json"
        ),
        "stage8_gold_version": "2.0.0",
        "stage8_gold_digest": _file_sha256(
            ROOT / "evaluation" / "gold-v1" / "manifest.json"
        ),
        "stage8_report_digest": "7" * 64,
        "stage8_report_semantic_digest": (
            "af94664fb502498b884eada4b27af892d13d73b9fcf66790601957e672cb126d"
        ),
        "api_version": "0.1.0",
        "graph_schema_version": "neo4j-migrations-001-through-005",
        "governance_policy_version": "graph-governance-catalog-1.0.0",
        "splitter_version": "load-record-splitter:v1",
        "extractor_version": "synthetic-load-document-entity-extractor:v1",
        "prompt_version": "grounded-answer-v1.3.0",
        "output_schema_version": "grounded-answer-output-v1.0.0",
        "embedding_provider": "fixture",
        "embedding_model": "deterministic-load-sparse",
        "embedding_revision": "load-v1.0",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "llm_provider": "local-reference-llm",
        "llm_model": "deterministic-grounded-answer",
        "llm_revision": "1.0.0",
        "index_version": "acl-partitioned-bm25-v2+exact-authorized-cosine-v1",
        "configuration_version": "1.0.5",
        "configuration_digest": _file_sha256(
            ROOT / "evaluation" / "production-reference-config.v1.json"
        ),
        "neo4j_image": "neo4j:5.26.12-community",
        "neo4j_image_digest": (
            "9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37"
        ),
        "python_version": "3.12.12",
        "hardware_profile": "neo4j-8cpu-3072mb-loopback-v1",
        "code_commit": "a" * 40,
    }
    runtime_environment = {
        "actual_memory_bytes": 3_072 * 1_024 * 1_024,
        "actual_memory_swap_bytes": 3_072 * 1_024 * 1_024,
        "actual_nano_cpus": 8_000_000_000,
        "actual_neo4j_image": versions["neo4j_image"],
        "actual_neo4j_image_id": "4" * 64,
        "actual_neo4j_repo_digest": versions["neo4j_image_digest"],
        "api_process_resource_limit": "host-default-unbounded",
        "code_commit": versions["code_commit"],
        "configured_heap_initial": "512m",
        "configured_heap_max": "1024m",
        "configured_pagecache": "512m",
        "configured_transaction_timeout": "300s",
        "database_initial_node_count": 0,
        "database_initial_relationship_count": 0,
        "host_cpu_count": 8,
        "host_memory_bytes": 8 * 1_024 * 1_024 * 1_024,
        "host_platform": "test-platform",
        "readiness_probe_status": "ready",
        "readiness_transaction_timeout_seconds": 5.0,
        "retrieval_transaction_timeout_seconds": 5.0,
    }
    ingestion_evidence = {
        "acl_coverage": {
            "access_groups": [
                "load-tenant-01-group-01",
                "load-tenant-01-public",
            ],
            "cross_tenant_active_chunks": 2_000,
            "cross_tenant_active_embeddings": 2_000,
            "denied_same_tenant_active_chunks": 2_500,
            "denied_same_tenant_active_embeddings": 2_500,
            "tenant_id": "load-tenant-01",
            "total_same_tenant_active_chunks": 10_000,
            "total_same_tenant_active_embeddings": 10_000,
            "visible_same_tenant_active_chunks": 7_500,
            "visible_same_tenant_active_embeddings": 7_500,
        },
        "active_generations": {
            "load-tenant-01": "bf694c4e-e7a9-5758-8418-56000e0b8774",
            "load-tenant-02": "b4df8766-d71b-5957-ab84-91ac606e9526",
            "load-tenant-03": "20396121-c96b-5866-8013-9b7ea45b8b12",
            "load-tenant-04": "6d5c490b-c56c-5cff-9aa9-a08908acb7cb",
            "load-tenant-05": "3ebac453-1366-55ee-9bab-437ed375007d",
        },
        "clean_start": True,
        "completed_monotonic_ms": 100_000.0,
        "completed_versions": 480,
        "database_documents": 240,
        "database_versions": 480,
        "embedding_generation_coverage": {
            "load-tenant-01": {
                "covered_chunks": 10_000,
                "generation_id": "bf694c4e-e7a9-5758-8418-56000e0b8774",
                "total_chunks": 10_000,
            },
            "load-tenant-02": {
                "covered_chunks": 500,
                "generation_id": "b4df8766-d71b-5957-ab84-91ac606e9526",
                "total_chunks": 500,
            },
            "load-tenant-03": {
                "covered_chunks": 500,
                "generation_id": "20396121-c96b-5866-8013-9b7ea45b8b12",
                "total_chunks": 500,
            },
            "load-tenant-04": {
                "covered_chunks": 500,
                "generation_id": "6d5c490b-c56c-5cff-9aa9-a08908acb7cb",
                "total_chunks": 500,
            },
            "load-tenant-05": {
                "covered_chunks": 500,
                "generation_id": "3ebac453-1366-55ee-9bab-437ed375007d",
                "total_chunks": 500,
            },
        },
        "failed_versions": 0,
        "idempotency_after_state_sha256": "sha256:" + "1" * 64,
        "idempotency_before_state_sha256": "sha256:" + "1" * 64,
        "idempotency_mismatch_count": 0,
        "initial_load_transaction_timeout_seconds": 60.0,
        "interrupted_after_state_sha256": "sha256:" + "f" * 64,
        "interrupted_before_state_sha256": "sha256:" + "f" * 64,
        "interrupted_job_count": 0,
        "interrupted_task_node_count": 0,
        "primary_tenant_active_chunks": 10_000,
        "primary_visible_chunks": 7_500,
        "recovered_job": {
            "attempts": 1,
            "built_chunk_count": 50,
            "built_embedding_count": 50,
            "built_snapshot_id": "a7c34cb5-b8eb-5edd-bdbb-441fea1ed6ce",
            "completed_tasks": 50,
            "corpus_revision": 1,
            "document_id": "c2af22e6-662b-51ed-a0c4-6637353f9509",
            "expected_active_snapshot_id": "",
            "expected_tasks": 50,
            "idempotency_key": "load-v1:load-tenant-02:document-001:v1",
            "job_id": "8a9ab702-be61-5826-8975-c4ae23dd84a9",
            "max_attempts": 1,
            "operation": "INITIAL_LOAD",
            "operation_key": "load-v1:load-tenant-02:document-001:v1",
            "outcome": "CREATED",
            "phase": "COMPLETE",
            "request_fingerprint": "sha256:"
            + "b5b2fac22d28a211d2bfef3b83823061f73f66c9189c2e9e5c5f53d219c9f1a9",
            "snapshot_expected_chunk_count": 50,
            "snapshot_manifest_hash": "sha256:"
            + "76d51d17de0bc312e0652a45ce16e706e7401bdc4fe3d6373ef2d89b65e99049",
            "source_generation": 0,
            "status": "SUCCEEDED",
            "target_snapshot_id": "a7c34cb5-b8eb-5edd-bdbb-441fea1ed6ce",
            "target_version_id": "40fc0422-cf7d-56f6-8557-c1bebb6462f8",
            "tenant_id": "load-tenant-02",
        },
        "recovered_job_id": "8a9ab702-be61-5826-8975-c4ae23dd84a9",
        "recovered_job_linked_task_count": 0,
        "recovered_job_task_node_count": 0,
        "recovery_checkpoint": "BEFORE_PUBLISH",
        "recovery_task_tracking_mode": "aggregate_job_counters",
        "replayed_active_versions": 240,
        "replay_completed_monotonic_ms": 100_002.0,
        "replay_started_monotonic_ms": 100_001.0,
        "query_ready_monotonic_ms": 100_003.0,
        "schema_version": "production-ingestion-observation-v2",
        "started_monotonic_ms": 0.0,
        "submitted_chunks": 24_000,
        "total_active_chunks": 12_000,
        "total_historical_chunks": 12_000,
        "total_versions": 480,
    }
    before_generation_snapshot = {
        **deepcopy(load_graph_expectations["before_generation_activation"]),
        "sha256": "sha256:" + "1" * 64,
    }
    query_ready_snapshot = {
        **deepcopy(load_graph_shape),
        "sha256": "sha256:" + "2" * 64,
    }
    load_graph_state = {
        "after_idempotent_replay": deepcopy(before_generation_snapshot),
        "before_idempotent_replay": deepcopy(before_generation_snapshot),
        "idempotency_mismatch_count": 0,
        "query_ready_state": deepcopy(query_ready_snapshot),
        "schema_version": "canonical-graph-state-observation-v2",
    }
    deletion_counts = {
        "Assertion": 0,
        "Chunk": 100,
        "ChunkEmbedding": 100,
        "Document": 1,
        "DocumentVersion": 2,
        "Entity": 1,
        "EntityMention": 100,
        "GraphGovernanceFinding": 0,
        "KnowledgeSnapshot": 2,
    }
    deletion_evidence = {
        "deletion_residue_count": 0,
        "delete_job_id": "f64db02e-2677-5864-b36a-ac6a6fa337ee",
        "document_id": "de30ee12-9343-5b6a-a626-9ee6d80754a2",
        "durable_audit_job_count": 3,
        "durable_audit_job_ids": [
            "c34132b4-ee97-56da-96f3-8754f93ecded",
            "f64db02e-2677-5864-b36a-ac6a6fa337ee",
            "fd242c44-6ee1-56d9-94e6-a74176a614e2",
        ],
        "durable_audit_jobs": [
            {
                "completed_tasks": 0,
                "document_id": "de30ee12-9343-5b6a-a626-9ee6d80754a2",
                "expected_tasks": 0,
                "job_id": "f64db02e-2677-5864-b36a-ac6a6fa337ee",
                "operation": "DELETE",
                "operation_key": "stage9-production-delete-validation",
                "outcome": "DELETED",
                "phase": "COMPLETE",
                "status": "SUCCEEDED",
                "target_snapshot_id": "",
                "target_version_id": "",
                "tenant_id": "load-tenant-05",
            },
            {
                "completed_tasks": 50,
                "document_id": "de30ee12-9343-5b6a-a626-9ee6d80754a2",
                "expected_tasks": 50,
                "job_id": "c34132b4-ee97-56da-96f3-8754f93ecded",
                "operation": "INITIAL_LOAD",
                "operation_key": "load-v1:load-tenant-05:document-010:v1",
                "outcome": "CREATED",
                "phase": "COMPLETE",
                "status": "SUCCEEDED",
                "target_snapshot_id": "93800a47-d3dd-5029-ac2e-3a300f78a069",
                "target_version_id": "d06370ca-d6a6-5ef0-973c-bfb5cf891144",
                "tenant_id": "load-tenant-05",
            },
            {
                "completed_tasks": 50,
                "document_id": "de30ee12-9343-5b6a-a626-9ee6d80754a2",
                "expected_tasks": 50,
                "job_id": "fd242c44-6ee1-56d9-94e6-a74176a614e2",
                "operation": "INITIAL_LOAD",
                "operation_key": "load-v1:load-tenant-05:document-010:v2",
                "outcome": "UPDATED",
                "phase": "COMPLETE",
                "status": "SUCCEEDED",
                "target_snapshot_id": "0ef4377c-bf62-5c14-9a70-6d024cc141c5",
                "target_version_id": "1236f688-d79b-5120-ad60-fd8f82610388",
                "tenant_id": "load-tenant-05",
            },
        ],
        "durable_audit_records_retained": True,
        "expected_removed_counts": deletion_counts,
        "observed_removed_counts": deletion_counts,
        "other_tenant_preserved": True,
        "preserved_tenant_ids": [
            "load-tenant-01",
            "load-tenant-02",
            "load-tenant-03",
            "load-tenant-04",
            "tenant-alpha",
            "tenant-beta",
        ],
        "residue_by_label": {label: 0 for label in deletion_counts},
        "schema_version": "production-deletion-observation-v1",
        "tenant_id": "load-tenant-05",
        "tombstone_generation": 1,
        "tombstone_deleted_by_job_id": (
            "f64db02e-2677-5864-b36a-ac6a6fa337ee"
        ),
        "target_active_chunk_ids": [
            "1a6ca0b8-67ba-5181-b0fc-55b4d6ce3e79",
            "7700d74f-1ba6-5160-846f-7ec1f2faa5dc",
        ],
        "target_active_snapshot_id": "0ef4377c-bf62-5c14-9a70-6d024cc141c5",
        "target_active_version_id": "1236f688-d79b-5120-ad60-fd8f82610388",
    }
    backup_evidence = {
        "backup_sha256": "d" * 64,
        "backup_size_bytes": 1_024,
        "container_resources_match": True,
        "database": "neo4j",
        "dump_command": (
            "neo4j-admin database dump neo4j --to-path=/backups "
            "--overwrite-destination=true"
        ),
        "load_command": (
            "neo4j-admin database load neo4j --from-path=/backups "
            "--overwrite-destination=true"
        ),
        "restored_business_node_count": canonical_graph_state[
            "business_node_count"
        ],
        "restored_business_relationship_count": canonical_graph_state[
            "business_relationship_count"
        ],
        "restored_container_resource_sha256": "e" * 64,
        "restored_state_sha256": "c" * 64,
        "schema_and_indexes_verified": True,
        "schema_version": "production-backup-restore-observation-v1",
        "source_business_node_count": canonical_graph_state[
            "business_node_count"
        ],
        "source_business_relationship_count": canonical_graph_state[
            "business_relationship_count"
        ],
        "source_container_resource_sha256": "e" * 64,
        "source_state_sha256": "c" * 64,
    }
    quality_cases, retrieval_quality, answer_quality = deepcopy(_quality_fixture())
    record_counts = {
        "fault_timeline": len(scenario_ids),
        "large_database_quality_cases": 49,
        "load_graph_state": 1,
        "load_corpus": 10_000,
        "request_samples": 2_430,
        "reference_answer_predictions": 49,
        "retrieval_stage_samples": 2_430,
        "suite_results": len(SUITES),
    }
    artifact_digests = {
        "acceptance_contract": versions["contract_digest"],
        "answer_embedding_corpus": versions["answer_embedding_corpus_digest"],
        "backup_dump": backup_evidence["backup_sha256"],
        "backup_observation": _sha256(
            {
                **backup_evidence,
                "backup_sha256": _prefixed(backup_evidence["backup_sha256"]),
                "restored_container_resource_sha256": _prefixed(
                    backup_evidence["restored_container_resource_sha256"]
                ),
                "restored_state_sha256": _prefixed(
                    backup_evidence["restored_state_sha256"]
                ),
                "source_state_sha256": _prefixed(
                    backup_evidence["source_state_sha256"]
                ),
                "source_container_resource_sha256": _prefixed(
                    backup_evidence["source_container_resource_sha256"]
                ),
            }
        ),
        "deletion_observation": _sha256(_normalized_deletion(deletion_evidence)),
        "ingestion_observation": _sha256(ingestion_evidence),
        "load_graph_state": _sha256(load_graph_state),
        "load_corpus": versions["load_corpus_digest"],
        "production_configuration": versions["configuration_digest"],
        "reference_answer_predictions": versions["answer_prediction_digest"],
        "stage8_report": versions["stage8_report_digest"],
        "validation_profile": versions["profile_digest"],
        "request_samples": _sha256(
            {
                section: [
                    {
                        **sample,
                        "query_vector_checksum": _prefixed(
                            sample["query_vector_checksum"]
                        ),
                    }
                    for sample in samples
                ]
                for section, samples in request_samples.items()
            }
        ),
        "retrieval_stage_samples": _sha256(
            sorted(
                (
                    {
                        "request_id": sample["request_id"],
                        "retrieval_stage_ms": sample["retrieval_stage_ms"],
                        "trace_id": sample["trace_id"],
                    }
                    for samples in request_samples.values()
                    for sample in samples
                ),
                key=lambda item: item["request_id"],
            )
        ),
        "fault_timeline": _sha256(fault_timeline),
        "suite_results": _sha256(suite_results),
        "graph_pre_state": _sha256(
            {**canonical_graph_state, "sha256": "sha256:" + "c" * 64}
        ),
        "graph_post_state": _sha256(
            {**canonical_graph_state, "sha256": "sha256:" + "c" * 64}
        ),
        "graph_backup_source_state": _sha256(
            {**canonical_graph_state, "sha256": "sha256:" + "c" * 64}
        ),
        "graph_restore_state": _sha256(
            {**canonical_graph_state, "sha256": "sha256:" + "c" * 64}
        ),
        "container_inspection": _sha256(
            {
                **runtime_environment,
                "actual_neo4j_image_id": _prefixed(
                    runtime_environment["actual_neo4j_image_id"]
                ),
                "actual_neo4j_repo_digest": _prefixed(
                    runtime_environment["actual_neo4j_repo_digest"]
                ),
            }
        ),
    }
    evidence_manifest = {
        evidence_id: {
            "path": f"evidence/{evidence_id}.json",
            "sha256": artifact_digests.get(evidence_id, "b" * 64),
            "record_count": record_counts.get(evidence_id, 1),
            "schema": schema,
        }
        for evidence_id, schema in EVIDENCE_SCHEMAS.items()
    }
    metrics = {
        "entity_precision": 1.0,
        "relationship_precision": 1.0,
        "entity_resolution_accuracy": 1.0,
        "recall_at_5": retrieval_quality["recall_at_5"],
        "mrr": retrieval_quality["mrr"],
        "ndcg_at_5": retrieval_quality["ndcg_at_5"],
        "unauthorized_exposure_count": 0,
        "supported_claim_rate": answer_quality["supported_claim_rate"],
        "citation_precision": answer_quality["citation_precision"],
        "citation_coverage": answer_quality["citation_coverage"],
        "numerical_fidelity": answer_quality["numerical_fidelity"],
        "refusal_f1": answer_quality["refusal_f1"],
        "ingestion_success_rate": 1.0,
        "idempotency_mismatch_count": 0,
        "deletion_residue_count": 0,
        "recovery_success_rate": 1.0,
        "retrieval_p95_ms": 1.0,
        "answer_p95_ms": 14_000.0,
        "retrieval_throughput_rps": 8.0,
        "server_error_rate": 0.0,
    }
    gold_manifest = _load(ROOT / "evaluation" / "gold-v1" / "manifest.json")
    quality_evidence = {
        "active_generation_ids": {
            "tenant-alpha": "fa9d6260-84b8-50bc-936a-b99d2b7ef817",
            "tenant-beta": "b6aaed8c-6907-5b3a-82a9-f63cd559b9e9",
        },
        "answer_retrieval_limits": asdict(PRODUCTION_ANSWER_RETRIEVAL_LIMITS),
        "answer_metrics": answer_quality,
        "case_count": 49,
        "case_evidence": quality_cases,
        "case_ids": gold_manifest["coverage"]["required_case_ids"],
        "case_set_sha256": gold_manifest["coverage"]["case_set_sha256"],
        "corpus_version": "1.0.1",
        "failures": [],
        "gold_projection_sha256": canonical_quality_digest(quality_cases),
        "graph_state_sha256": "sha256:" + "c" * 64,
        "passed": True,
        "prediction_provider": REFERENCE_PREDICTION_PROVIDER,
        "prediction_sha256": "sha256:" + REFERENCE_PREDICTION_SHA256,
        "prediction_version": REFERENCE_PREDICTION_VERSION,
        "production_configuration_sha256": (
            "sha256:"
            + _file_sha256(
                ROOT / "evaluation" / "production-reference-config.v1.json"
            )
        ),
        "retrieval_metrics": retrieval_quality,
        "schema_version": "production-large-database-quality-v1",
    }
    evidence_manifest["large_database_quality"]["sha256"] = _sha256(
        {
            **quality_evidence,
            "case_set_sha256": _prefixed(quality_evidence["case_set_sha256"]),
            "gold_projection_sha256": _prefixed(
                quality_evidence["gold_projection_sha256"]
            ),
        }
    )
    evidence_manifest["large_database_quality_cases"]["sha256"] = _sha256(
        quality_cases
    )
    total_cost = math.fsum([0.001] * 30)
    evidence_manifest["provider_usage"]["sha256"] = _sha256(
        {
            "cost": {
                "currency": "USD",
                "estimated_total_usd": total_cost,
                "input_tokens": 32_040,
                "mean_request_usd": total_cost / 30,
                "metered_requests": 30,
                "model_calls": 2_460,
                "output_tokens": 1_440,
                "request_cost_sample_count": 30,
            },
            "embedding_latency_ms": [20.0] * 2_430,
            "llm_latency_ms": sorted(
                [500.0] * 15 + [1_000.0] * 14 + [2_000.0]
            ),
            "provider_evidence": {
                "answer_preflight_case_ids": answer_preflight_case_ids,
                "answer_warmup_model_calls": 30,
                "measured_answer_model_calls": 30,
                "measured_embedding_model_calls": 2_430,
                "mode": "deterministic_reference",
                "peak_concurrency": 8,
            },
        }
    )
    return {
        "backup_evidence": backup_evidence,
        "schema_version": PRODUCTION_OBSERVATION_SCHEMA_VERSION,
        "profile_id": "production-reference",
        "workload": {
            "chunk_count": 10_000,
            "concurrency": 8,
            "sustained_seconds": 300,
            "answer_samples": 30,
            "warmed": True,
        },
        "metrics": metrics,
        "deletion_evidence": deletion_evidence,
        "ingestion_evidence": ingestion_evidence,
        "load_graph_state": load_graph_state,
        "request_samples": request_samples,
        "latency_ms": {
            "ingestion": [10.0] * 50 + [20.0] * 45 + [30.0] * 5,
            "retrieval_stage_ms": [1.0] * 2_430,
            "embedding_provider": [20.0] * 2_430,
            "llm": [500.0] * 15 + [1_000.0] * 14 + [2_000.0],
        },
        "traffic": {
            "ingestion_chunks": 24_000,
            "ingestion_started_monotonic_ms": 0,
            "ingestion_completed_monotonic_ms": 100_000,
        },
        "cost": {
            "currency": "USD",
            "request_cost_usd": [0.001] * 30,
            "model_calls": 2_460,
            "input_tokens": 32_040,
            "output_tokens": 1_440,
        },
        "scenarios": scenarios,
        "fault_timeline": fault_timeline,
        "suite_results": suite_results,
        "canonical_graph": canonical_graph,
        "runtime_environment": runtime_environment,
        "provider_evidence": {
            "answer_preflight_case_ids": answer_preflight_case_ids,
            "answer_warmup_model_calls": 30,
            "measured_answer_model_calls": 30,
            "measured_embedding_model_calls": 2_430,
            "mode": "deterministic_reference",
            "peak_concurrency": 8,
        },
        "quality_evidence": quality_evidence,
        "evidence_manifest": evidence_manifest,
        "versions": versions,
        "limitations": ["Validation is bounded to the declared reference workload."],
        "residual_risks": [
            "A deployment still requires environment-specific capacity review."
        ],
        "deployment_prerequisites": list(REQUIRED_DEPLOYMENT_PREREQUISITES),
    }


class ProductionEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = _load(ROOT / "contracts" / "acceptance.v1.json")
        cls.profile = _load(
            ROOT / "contracts" / "profiles" / "production-reference.v1.json"
        )

    def report(self, observations: dict | None = None) -> dict:
        return build_production_candidate_report(
            observations or _passing_observations(),
            self.contract,
            self.profile,
        )

    def test_passing_report_uses_raw_requests_and_all_twenty_gates(self) -> None:
        report = self.report()
        self.assertTrue(report["passed"])
        self.assertTrue(report["production_candidate_eligible"])
        self.assertEqual(report["failures"], [])
        rows = {item["id"]: item for item in report["contract_metrics"]}
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(item["passed"] for item in rows.values()))
        self.assertEqual(rows["retrieval_p95_ms"]["observed"], 1.0)

        self.assertEqual(
            report["latency_percentiles_ms"]["retrieval"],
            {"p50": 100.0, "p95": 900.0, "p99": 1_200.0, "sample_count": 2_400},
        )
        answer = report["latency_percentiles_ms"]["answer"]
        self.assertEqual(
            (answer["p50"], answer["p95"], answer["p99"]),
            (1_000.0, 14_000.0, 16_000.0),
        )
        self.assertEqual(
            report["latency_percentiles_ms"]["retrieval_stage_ms"],
            {"p50": 1.0, "p95": 1.0, "p99": 1.0, "sample_count": 2_430},
        )
        self.assertEqual(report["throughput"]["retrieval_requests_per_second"], 8.0)
        self.assertEqual(report["throughput"]["ingestion_chunks_per_second"], 240.0)
        self.assertEqual(report["request_diagnostics"]["measured_duration_seconds"], 300)
        self.assertEqual(report["request_diagnostics"]["peak_active_requests"], 8)
        self.assertLess(
            report["request_diagnostics"]["maximum_client_idle_ms"], 1_000
        )
        self.assertEqual(
            report["request_diagnostics"]["status_counts"],
            {"2xx": 2_400, "4xx": 0, "429": 0, "5xx": 0, "total": 2_400},
        )
        self.assertAlmostEqual(report["operating_cost"]["estimated_total_usd"], 0.03)
        self.assertRegex(report["semantic_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            report["evidence_manifest"]["request_samples"]["record_count"], 2_430
        )
        self.assertEqual(
            report["request_diagnostics"]["retrieval_stage_sample_count"], 2_430
        )
        self.assertEqual(
            {
                field: report["scenarios"]["llm_failure"][field]
                for field in ("http_status", "error_code", "domain_status", "reason")
            },
            {
                "http_status": 200,
                "error_code": None,
                "domain_status": "refused",
                "reason": "invalid_model_output",
            },
        )
        self.assertTrue(
            any(
                "deterministic reference envelope" in item
                for item in report["limitations"]
            )
        )
        self.assertTrue(
            any(
                "external embedding and LLM" in item
                for item in report["deployment_prerequisites"]
            )
        )
        prerequisites = "\n".join(report["deployment_prerequisites"])
        for required_phrase in (
            "workload identity",
            "customer corpora",
            "cluster failover",
            "distributed rate-limit",
            "on-call ownership",
            "privacy",
        ):
            with self.subTest(deployment_prerequisite=required_phrase):
                self.assertIn(required_phrase, prerequisites)

    def test_report_is_canonical_across_input_and_sample_order(self) -> None:
        first = _passing_observations()
        second = deepcopy(first)
        for field in (
            "metrics",
            "scenarios",
            "fault_timeline",
            "suite_results",
            "versions",
            "evidence_manifest",
        ):
            second[field] = dict(reversed(list(second[field].items())))
        for section in second["request_samples"].values():
            section.reverse()
        for samples in second["latency_ms"].values():
            samples.reverse()
        second["limitations"].reverse()
        second["residual_risks"].reverse()
        second["deployment_prerequisites"].reverse()
        self.assertEqual(self.report(first), self.report(second))

    def test_valid_failures_cannot_claim_qualification(self) -> None:
        observations = _passing_observations()
        observations["scenarios"]["backup_restore"]["passed"] = False
        observations["fault_timeline"]["backup_restore"]["assertion_failures"] = [
            "restore digest mismatch"
        ]
        observations["suite_results"]["quality"]["passed_test_ids"] = []
        observations["suite_results"]["quality"]["skipped_test_ids"] = [
            "quality.test_reference"
        ]
        observations["canonical_graph"]["post_validation_state"][
            "sha256"
        ] = "d" * 64
        observations["runtime_environment"]["database_initial_node_count"] = 1
        _rebind_raw(observations)

        report = self.report(observations)
        self.assertFalse(report["passed"])
        self.assertFalse(report["production_candidate_eligible"])
        joined = "\n".join(report["failures"])
        self.assertIn("backup_restore did not pass", joined)
        self.assertIn("suite quality", joined)
        self.assertIn("canonical graph changed", joined)
        self.assertIn("database was not clean", joined)

    def test_workload_and_actual_measured_window_are_strict(self) -> None:
        cases = {
            "chunk_count": 9_999,
            "concurrency": 7,
            "sustained_seconds": 299.999,
            "answer_samples": 29,
            "warmed": False,
        }
        for field, invalid in cases.items():
            with self.subTest(field=field):
                observations = _passing_observations()
                observations["workload"][field] = invalid
                with self.assertRaises(ValueError):
                    self.report(observations)

        padded = _passing_observations()
        padded["request_samples"]["retrieval"][-1]["completed_monotonic_ms"] -= 1
        with self.assertRaisesRegex(
            ValueError, "request timeline|sustain five minutes"
        ):
            self.report(padded)

        missing_client = _passing_observations()
        for sample in missing_client["request_samples"]["retrieval"]:
            if sample["client_id"] == "retrieval-07":
                sample["client_id"] = "retrieval-06"
        with self.assertRaisesRegex(ValueError, r"exact(?:ly)? eight client"):
            self.report(missing_client)

        serialized = _passing_observations()
        for index, sample in enumerate(
            serialized["request_samples"]["retrieval"]
        ):
            sample["started_monotonic_ms"] = index * 125.0
            sample["completed_monotonic_ms"] = index * 125.0 + 100.0
        serialized["request_samples"]["retrieval"][-1][
            "completed_monotonic_ms"
        ] = 300_000.0
        serialized["metrics"]["retrieval_p95_ms"] = 1.0
        with self.assertRaisesRegex(ValueError, "eight concurrent calls"):
            self.report(serialized)

        wrong_rotation = _passing_observations()
        client_samples = sorted(
            (
                item
                for item in wrong_rotation["request_samples"]["retrieval"]
                if item["client_id"] == "retrieval-00"
            ),
            key=lambda item: item["started_monotonic_ms"],
        )
        identity_fields = (
            "case_id",
            "embedding_space_id",
            "expected_chunk_ids",
            "query_vector_checksum",
            "selected_chunk_ids",
            "visible_chunk_ids",
        )
        for field in identity_fields:
            client_samples[0][field], client_samples[1][field] = (
                client_samples[1][field],
                client_samples[0][field],
            )
        with self.assertRaisesRegex(ValueError, "load-v1 round-robin"):
            self.report(wrong_rotation)

    def test_runtime_resource_envelope_rejects_the_retired_two_cpu_profile(self) -> None:
        observations = _passing_observations()
        self.assertTrue(self.report(observations)["passed"])
        observations["runtime_environment"]["actual_nano_cpus"] = 2_000_000_000
        with self.assertRaisesRegex(ValueError, "resource envelope drifted"):
            self.report(observations)

        undersized_host = _passing_observations()
        undersized_host["runtime_environment"]["host_cpu_count"] = 7
        with self.assertRaisesRegex(ValueError, "host CPU count"):
            self.report(undersized_host)

    def test_raw_statuses_include_4xx_and_429_and_derive_server_errors(self) -> None:
        observations = _passing_observations()
        statuses = [400, 429, 503]
        for sample, status in zip(
            observations["request_samples"]["retrieval"], statuses
        ):
            sample["status_code"] = status
            sample["semantic_success"] = False
            sample["error_code"] = {
                400: "invalid_request",
                429: "rate_limited",
                503: "dependency_unavailable",
            }[status]
        observations["metrics"]["retrieval_throughput_rps"] = 2_397 / 300
        observations["metrics"]["server_error_rate"] = 1 / 2_400
        _rebind_raw(observations)

        report = self.report(observations)
        self.assertEqual(
            report["request_diagnostics"]["status_counts"],
            {"2xx": 2_397, "4xx": 2, "429": 1, "5xx": 1, "total": 2_400},
        )
        self.assertAlmostEqual(
            report["throughput"]["retrieval_requests_per_second"], 7.99
        )
        self.assertAlmostEqual(
            report["throughput"]["server_error_rate"], 1 / 2_400
        )
        self.assertFalse(report["passed"])
        self.assertIn("retrieval_throughput_rps", "\n".join(report["failures"]))

        invalid = _passing_observations()
        invalid["request_samples"]["retrieval"][0]["status_code"] = 302
        with self.assertRaisesRegex(ValueError, "2xx, 4xx, or 5xx"):
            self.report(invalid)

    def test_summary_performance_values_cannot_override_raw_samples(self) -> None:
        for metric_id in (
            "entity_precision",
            "relationship_precision",
            "entity_resolution_accuracy",
        ):
            with self.subTest(metric_id=metric_id, source="stage8"):
                observations = _passing_observations()
                observations["metrics"][metric_id] = 0.99
                with self.assertRaisesRegex(ValueError, "reviewed Stage 8"):
                    self.report(observations)

        for metric_id, value in (
            ("retrieval_p95_ms", 899),
            ("answer_p95_ms", 13_999),
            ("retrieval_throughput_rps", 8.1),
            ("server_error_rate", 0.001),
        ):
            with self.subTest(metric_id=metric_id):
                observations = _passing_observations()
                observations["metrics"][metric_id] = value
                with self.assertRaisesRegex(ValueError, "independently calculated"):
                    self.report(observations)

        slow = _passing_observations()
        per_client_index: dict[str, int] = {}
        for sample in slow["request_samples"]["retrieval"]:
            client_id = sample["client_id"]
            ordinal = per_client_index.get(client_id, 0)
            sample["started_monotonic_ms"] = 200_000.0 + ordinal * 1_200.0
            sample["completed_monotonic_ms"] = (
                200_000.0 + (ordinal + 1) * 1_200.0
            )
            per_client_index[client_id] = ordinal + 1
        for sample in slow["request_samples"]["answer"]:
            sample["completed_monotonic_ms"] = (
                sample["started_monotonic_ms"] + 16_000
            )
        slow["metrics"]["retrieval_p95_ms"] = 1.0
        slow["metrics"]["answer_p95_ms"] = 16_000
        slow["metrics"]["retrieval_throughput_rps"] = 2_400 / 360
        slow["workload"]["sustained_seconds"] = 360
        _rebind_raw(slow)
        report = self.report(slow)
        self.assertFalse(report["passed"])
        failures = "\n".join(report["failures"])
        self.assertNotIn("retrieval_p95_ms", failures)
        self.assertIn("answer_p95_ms", failures)

        forged_backend = _passing_observations()
        for sample in forged_backend["request_samples"]["retrieval"]:
            sample["retrieval_stage_ms"] = 2.0
        forged_backend["latency_ms"]["retrieval_stage_ms"] = (
            [2.0] * 2_400 + [1.0] * 30
        )
        _rebind_raw(forged_backend)
        with self.assertRaisesRegex(ValueError, "independently calculated"):
            self.report(forged_backend)

    def test_evidence_manifest_is_exact_bound_and_path_safe(self) -> None:
        mutations: list[dict] = []
        missing = _passing_observations()
        missing["evidence_manifest"].pop("stage8_report")
        mutations.append(missing)
        extra = _passing_observations()
        extra["evidence_manifest"]["unreviewed"] = deepcopy(
            extra["evidence_manifest"]["stage8_report"]
        )
        mutations.append(extra)
        wrong_digest = _passing_observations()
        wrong_digest["evidence_manifest"]["stage8_report"]["sha256"] = "e" * 64
        mutations.append(wrong_digest)
        wrong_backup_dump = _passing_observations()
        wrong_backup_dump["evidence_manifest"]["backup_dump"]["sha256"] = "a" * 64
        mutations.append(wrong_backup_dump)
        wrong_count = _passing_observations()
        wrong_count["evidence_manifest"]["request_samples"]["record_count"] -= 1
        mutations.append(wrong_count)
        wrong_schema = _passing_observations()
        wrong_schema["evidence_manifest"]["fault_timeline"]["schema"] = "unknown"
        mutations.append(wrong_schema)
        unsafe_path = _passing_observations()
        unsafe_path["evidence_manifest"]["suite_results"]["path"] = "../escape.json"
        mutations.append(unsafe_path)
        unbound_raw = _passing_observations()
        unbound_raw["request_samples"]["answer"][0]["started_monotonic_ms"] += 1
        unbound_raw["request_samples"]["answer"][0]["completed_monotonic_ms"] += 1
        mutations.append(unbound_raw)

        for index, observations in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.report(observations)

    def test_fault_timeline_and_suite_coverage_cannot_be_forged(self) -> None:
        missing_scenario = _passing_observations()
        missing_scenario["scenarios"].pop("neo4j_failure")
        with self.assertRaisesRegex(ValueError, "missing"):
            self.report(missing_scenario)

        extra_scenario = _passing_observations()
        extra_scenario["fault_timeline"]["unreviewed"] = {
            "started_monotonic_ms": 0,
            "completed_monotonic_ms": 1,
            "http_status": 200,
            "error_code": None,
            "domain_status": None,
            "reason": None,
            "assertion_failures": [],
        }
        with self.assertRaisesRegex(ValueError, "extra"):
            self.report(extra_scenario)

        mismatch = _passing_observations()
        mismatch["fault_timeline"]["llm_timeout"]["error_code"] = "internal_error"
        with self.assertRaisesRegex(ValueError, "does not match fault timeline"):
            self.report(mismatch)

        forged_llm_failure = _passing_observations()
        wrong_outcome = {
            "http_status": 503,
            "error_code": "dependency_unavailable",
            "domain_status": None,
            "reason": None,
        }
        forged_llm_failure["scenarios"]["llm_failure"].update(wrong_outcome)
        forged_llm_failure["fault_timeline"]["llm_failure"].update(wrong_outcome)
        _rebind_raw(forged_llm_failure)
        report = self.report(forged_llm_failure)
        self.assertFalse(report["passed"])
        llm_failures = "\n".join(report["failures"])
        self.assertIn("llm_failure returned HTTP 503", llm_failures)
        self.assertIn("invalid_model_output", llm_failures)

        forged_pass = _passing_observations()
        forged_pass["fault_timeline"]["backup_restore"]["assertion_failures"] = [
            "restore failed"
        ]
        with self.assertRaisesRegex(ValueError, "pass flag"):
            self.report(forged_pass)

        omitted_test = _passing_observations()
        omitted_test["suite_results"]["security"]["passed_test_ids"] = []
        with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
            self.report(omitted_test)

        nested_eligibility = _passing_observations()
        nested_eligibility["provider_evidence"]["production_candidate_eligible"] = True
        with self.assertRaisesRegex(ValueError, "output-only"):
            self.report(nested_eligibility)

        early_provider_timeout = _passing_observations()
        early_provider_timeout["scenarios"]["llm_timeout"]["latency_ms"] = 75.0
        early_provider_timeout["fault_timeline"]["llm_timeout"][
            "completed_monotonic_ms"
        ] = (
            early_provider_timeout["fault_timeline"]["llm_timeout"][
                "started_monotonic_ms"
            ]
            + 75.0
        )
        with self.assertRaisesRegex(ValueError, "five-second API deadline"):
            self.report(early_provider_timeout)

        forged_access_inventory = _passing_observations()
        forged_access_inventory["fault_timeline"]["access_isolation"][
            "access_evidence"
        ]["inventory"]["authorized"]["count"] += 1
        with self.assertRaisesRegex(ValueError, "inventory does not match load-v1"):
            self.report(forged_access_inventory)

        leaked_canary = _passing_observations()
        access_probe = leaked_canary["fault_timeline"]["access_isolation"][
            "access_evidence"
        ]["probes"][0]
        canary_id = access_probe["canary_chunk_id"]
        access_probe["response"]["trace"]["vector_recall"] = [
            {"chunk_id": canary_id, "rank": 1, "score": 1.0}
        ]
        access_probe["trace_chunk_ids"] = [canary_id]
        with self.assertRaisesRegex(ValueError, "protected existence signal"):
            self.report(leaked_canary)

    def test_missing_extra_nonfinite_and_qualification_are_rejected(self) -> None:
        mutations: list[dict] = []
        missing_metric = _passing_observations()
        missing_metric["metrics"].pop("mrr")
        mutations.append(missing_metric)
        extra_metric = _passing_observations()
        extra_metric["metrics"]["invented_score"] = 1.0
        mutations.append(extra_metric)
        nonfinite_metric = _passing_observations()
        nonfinite_metric["metrics"]["mrr"] = math.nan
        mutations.append(nonfinite_metric)
        nonfinite_sample = _passing_observations()
        nonfinite_sample["request_samples"]["retrieval"][0][
            "completed_monotonic_ms"
        ] = math.inf
        mutations.append(nonfinite_sample)
        extra_sample_field = _passing_observations()
        extra_sample_field["request_samples"]["retrieval"][0]["latency_ms"] = 1
        mutations.append(extra_sample_field)
        forged = _passing_observations()
        forged["production_candidate_eligible"] = True
        mutations.append(forged)

        for index, observations in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.report(observations)

    def test_provider_mode_is_fail_closed_to_the_versioned_reference(self) -> None:
        deterministic = self.report()
        self.assertTrue(deterministic["passed"])
        self.assertTrue(
            any(
                "deterministic reference envelope" in item
                for item in deterministic["limitations"]
            )
        )

        for unsupported in ("live_external", "claimed_live"):
            with self.subTest(mode=unsupported):
                invalid = _passing_observations()
                invalid["provider_evidence"]["mode"] = unsupported
                with self.assertRaisesRegex(ValueError, "mode is invalid"):
                    self.report(invalid)

        invalid_warmup = _passing_observations()
        invalid_warmup["provider_evidence"]["answer_warmup_model_calls"] = 0
        with self.assertRaisesRegex(ValueError, "full answer preflight"):
            self.report(invalid_warmup)

        invalid_preflight_set = _passing_observations()
        preflight_ids = invalid_preflight_set["provider_evidence"][
            "answer_preflight_case_ids"
        ]
        preflight_ids[-1] = "forged-answer-case"
        with self.assertRaisesRegex(ValueError, "fixed answer case set"):
            self.report(invalid_preflight_set)

    def test_request_identity_and_provider_latency_coverage_are_recomputed(self) -> None:
        wrong_anchor = _passing_observations()
        sample = wrong_anchor["request_samples"]["retrieval"][0]
        sample["selected_chunk_ids"] = ["different-authorized-chunk"]
        sample["visible_chunk_ids"] = ["different-authorized-chunk"]
        with self.assertRaisesRegex(ValueError, "semantic result is inconsistent"):
            self.report(wrong_anchor)

        wrong_dataset = _passing_observations()
        wrong_dataset["request_samples"]["answer"][0]["dataset_id"] = "load-v1"
        with self.assertRaisesRegex(ValueError, "fixed gold-v1 cases"):
            self.report(wrong_dataset)

        duplicate_case = _passing_observations()
        answer_samples = duplicate_case["request_samples"]["answer"]
        answer_samples[0]["case_id"] = answer_samples[1]["case_id"]
        with self.assertRaisesRegex(ValueError, "case IDs must be unique"):
            self.report(duplicate_case)

        forged_expectation = _passing_observations()
        answer = forged_expectation["request_samples"]["answer"][0]
        answer["expected_chunk_ids"] = ["forged-expected-chunk"]
        answer["selected_chunk_ids"] = ["forged-expected-chunk"]
        answer["visible_chunk_ids"] = ["forged-expected-chunk"]
        with self.assertRaisesRegex(ValueError, "expectations do not match gold-v1"):
            self.report(forged_expectation)

        forged_embedding = _passing_observations()
        forged_embedding["request_samples"]["answer"][0][
            "embedding_space_id"
        ] = "forged-space"
        with self.assertRaisesRegex(ValueError, "expectations do not match gold-v1"):
            self.report(forged_embedding)

        forged_query_vector = _passing_observations()
        forged_query_vector["request_samples"]["retrieval"][0][
            "query_vector_checksum"
        ] = "0" * 64
        with self.assertRaisesRegex(ValueError, "expectations changed"):
            self.report(forged_query_vector)

        wrong_claim_commitment = _passing_observations()
        wrong_claim_commitment["request_samples"]["answer"][0][
            "answer_evidence"
        ]["claims"][0]["text_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "claim commitment is not gold"):
            self.report(wrong_claim_commitment)

        wrong_citation_commitment = _passing_observations()
        wrong_citation_commitment["request_samples"]["answer"][0][
            "answer_evidence"
        ]["citations"][0]["citation_id"] = "S99"
        with self.assertRaisesRegex(ValueError, "unknown citations|all be referenced"):
            self.report(wrong_citation_commitment)

        wrong_location_commitment = _passing_observations()
        wrong_location_commitment["request_samples"]["answer"][0][
            "answer_evidence"
        ]["citations"][0]["location_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "citation location is invalid"):
            self.report(wrong_location_commitment)

        missing_embedding_latency = _passing_observations()
        missing_embedding_latency["latency_ms"]["embedding_provider"].pop()
        with self.assertRaisesRegex(ValueError, "do not cover model calls"):
            self.report(missing_embedding_latency)

        missing_llm_latency = _passing_observations()
        missing_llm_latency["latency_ms"]["llm"].pop()
        with self.assertRaisesRegex(ValueError, "do not cover model calls"):
            self.report(missing_llm_latency)

        missing_retrieval_stage = _passing_observations()
        missing_retrieval_stage["latency_ms"]["retrieval_stage_ms"].pop()
        with self.assertRaisesRegex(ValueError, "exactly cover measured requests"):
            self.report(missing_retrieval_stage)

        out_of_bounds_retrieval_stage = _passing_observations()
        request = out_of_bounds_retrieval_stage["request_samples"]["retrieval"][0]
        request["retrieval_stage_ms"] = 101.0
        with self.assertRaisesRegex(ValueError, "cannot exceed HTTP request duration"):
            self.report(out_of_bounds_retrieval_stage)

    def test_quality_raw_cases_reject_tampering_and_incomplete_coverage(self) -> None:
        missing = _passing_observations()
        missing["quality_evidence"]["case_evidence"]["cases"].pop()
        with self.assertRaisesRegex(ValueError, "exact 49-case gold set"):
            self.report(missing)

        forged_claim = _passing_observations()
        answered_case = next(
            item
            for item in forged_claim["quality_evidence"]["case_evidence"]["cases"]
            if item["answer"]["claims"]
        )
        answered_case["answer"]["claims"][0]["text_sha256"] = "sha256:" + "f" * 64
        forged_claim["quality_evidence"][
            "gold_projection_sha256"
        ] = canonical_quality_digest(
            forged_claim["quality_evidence"]["case_evidence"]
        )
        _rebind_raw(forged_claim)
        with self.assertRaisesRegex(ValueError, "claim text digest is invalid"):
            self.report(forged_claim)

        forged_aggregate = _passing_observations()
        forged_aggregate["quality_evidence"]["retrieval_metrics"]["mrr"] = 0.99
        forged_aggregate["metrics"]["mrr"] = 0.99
        _rebind_raw(forged_aggregate)
        with self.assertRaisesRegex(ValueError, "does not match raw cases"):
            self.report(forged_aggregate)

        incomplete_answer = _passing_observations()
        quality = incomplete_answer["quality_evidence"]
        raw_cases = quality["case_evidence"]
        case = next(
            item
            for item in raw_cases["cases"]
            if item["id"] == "cross_chunk-boundary-01"
        )
        case["answer"]["claims"].pop()
        referenced_citations = {
            citation_id
            for claim in case["answer"]["claims"]
            for citation_id in claim["citation_ids"]
        }
        case["answer"]["citations"] = [
            citation
            for citation in case["answer"]["citations"]
            if citation["citation_id"] in referenced_citations
        ]
        gold_answers = {
            item["id"]: item
            for item in map(
                json.loads,
                (ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl")
                .read_text(encoding="utf-8")
                .splitlines(),
            )
        }
        gold_claims = {
            item["claim_id"]: item
            for item in gold_answers[case["id"]]["claims"]
        }
        rendered = "\n".join(
            f"{'Inference: ' if claim['inference'] else ''}"
            f"{gold_claims[claim['claim_id']]['reference_text']} "
            + " ".join(f"[{item}]" for item in claim["citation_ids"])
            for claim in case["answer"]["claims"]
        )
        case["answer"]["answer_sha256"] = "sha256:" + hashlib.sha256(
            rendered.encode("utf-8")
        ).hexdigest()
        normalized, retrieval_metrics, answer_metrics = (
            evaluate_quality_case_evidence(raw_cases)
        )
        quality["case_evidence"] = normalized
        quality["retrieval_metrics"] = retrieval_metrics
        quality["answer_metrics"] = answer_metrics
        quality["gold_projection_sha256"] = canonical_quality_digest(normalized)
        for metric_id in (
            "recall_at_5",
            "mrr",
            "ndcg_at_5",
            "unauthorized_exposure_count",
        ):
            incomplete_answer["metrics"][metric_id] = retrieval_metrics[metric_id]
        for metric_id in (
            "supported_claim_rate",
            "citation_precision",
            "citation_coverage",
            "numerical_fidelity",
            "refusal_f1",
        ):
            incomplete_answer["metrics"][metric_id] = answer_metrics[metric_id]
        _rebind_raw(incomplete_answer)
        with self.assertRaisesRegex(ValueError, "does not match measured metrics"):
            self.report(incomplete_answer)

        forged_generation = _passing_observations()
        forged_generation["quality_evidence"]["active_generation_ids"][
            "tenant-alpha"
        ] = "forged-generation"
        _rebind_raw(forged_generation)
        with self.assertRaisesRegex(ValueError, "reviewed corpus"):
            self.report(forged_generation)

        detached_graph = _passing_observations()
        detached_graph["quality_evidence"]["graph_state_sha256"] = "d" * 64
        _rebind_raw(detached_graph)
        with self.assertRaisesRegex(ValueError, "canonical graph state"):
            self.report(detached_graph)

        forged_prediction = _passing_observations()
        forged_prediction["quality_evidence"]["prediction_sha256"] = (
            "sha256:" + "0" * 64
        )
        _rebind_raw(forged_prediction)
        with self.assertRaisesRegex(ValueError, "reviewed artifact"):
            self.report(forged_prediction)

        forged_answer_profile = _passing_observations()
        forged_answer_profile["quality_evidence"]["answer_retrieval_limits"][
            "seed_k"
        ] = 2
        _rebind_raw(forged_answer_profile)
        with self.assertRaisesRegex(ValueError, "answer profile is not reviewed"):
            self.report(forged_answer_profile)

        forged_configuration = _passing_observations()
        forged_configuration["quality_evidence"][
            "production_configuration_sha256"
        ] = "sha256:" + "0" * 64
        _rebind_raw(forged_configuration)
        with self.assertRaisesRegex(ValueError, "reviewed file"):
            self.report(forged_configuration)

    def test_quality_raw_cases_do_not_retain_answer_or_location_text(self) -> None:
        cases = _passing_observations()["quality_evidence"]["case_evidence"][
            "cases"
        ]
        serialized = json.dumps(cases, sort_keys=True)
        gold_answer = json.loads(
            (ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertNotIn(gold_answer["reference_answer"], serialized)
        self.assertNotIn(gold_answer["evidence"][0]["document_title"], serialized)
        self.assertNotIn("answer", cases[0]["answer"])

    def test_ingestion_and_deletion_claims_require_independent_raw_evidence(self) -> None:
        zero_deletion = _passing_observations()
        zero_counts = {
            label: 0
            for label in zero_deletion["deletion_evidence"][
                "expected_removed_counts"
            ]
        }
        zero_deletion["deletion_evidence"]["expected_removed_counts"] = zero_counts
        zero_deletion["deletion_evidence"]["observed_removed_counts"] = zero_counts
        _rebind_raw(zero_deletion)
        with self.assertRaisesRegex(ValueError, "fixed non-empty candidate"):
            self.report(zero_deletion)

        missing_history = _passing_observations()
        missing_history["ingestion_evidence"]["total_historical_chunks"] = 0
        _rebind_raw(missing_history)
        with self.assertRaisesRegex(ValueError, "total_historical_chunks"):
            self.report(missing_history)

        unbounded_initial_load = _passing_observations()
        unbounded_initial_load["ingestion_evidence"][
            "initial_load_transaction_timeout_seconds"
        ] = 300.0
        _rebind_raw(unbounded_initial_load)
        with self.assertRaisesRegex(ValueError, "initial-load transaction timeout"):
            self.report(unbounded_initial_load)

        for field, invalid in (
            ("retrieval_transaction_timeout_seconds", 60.0),
            ("readiness_transaction_timeout_seconds", 300.0),
        ):
            with self.subTest(online_timeout=field):
                unbounded_online = _passing_observations()
                unbounded_online["runtime_environment"][field] = invalid
                _rebind_raw(unbounded_online)
                with self.assertRaisesRegex(
                    ValueError, "online Neo4j transaction timeout"
                ):
                    self.report(unbounded_online)

        failed_readiness = _passing_observations()
        failed_readiness["runtime_environment"][
            "readiness_probe_status"
        ] = "not_ready"
        _rebind_raw(failed_readiness)
        with self.assertRaisesRegex(ValueError, "readiness probe did not pass"):
            self.report(failed_readiness)

        unbound_replay_timeline = _passing_observations()
        unbound_replay_timeline["fault_timeline"]["idempotency"].update(
            {
                "started_monotonic_ms": 600_000.0,
                "completed_monotonic_ms": 600_001.0,
            }
        )
        _rebind_raw(unbound_replay_timeline)
        with self.assertRaisesRegex(ValueError, "does not bind ingestion replay"):
            self.report(unbound_replay_timeline)

        post_request_query_ready = _passing_observations()
        post_request_query_ready["ingestion_evidence"].update(
            {
                "replay_started_monotonic_ms": 600_000.0,
                "replay_completed_monotonic_ms": 600_001.0,
                "query_ready_monotonic_ms": 600_002.0,
            }
        )
        post_request_query_ready["fault_timeline"]["idempotency"].update(
            {
                "started_monotonic_ms": 600_000.0,
                "completed_monotonic_ms": 600_001.0,
            }
        )
        _rebind_raw(post_request_query_ready)
        with self.assertRaisesRegex(ValueError, "precede every measured HTTP"):
            self.report(post_request_query_ready)

        equal_lifecycle_boundary = _passing_observations()
        equal_lifecycle_boundary["ingestion_evidence"][
            "replay_started_monotonic_ms"
        ] = equal_lifecycle_boundary["ingestion_evidence"][
            "completed_monotonic_ms"
        ]
        equal_lifecycle_boundary["fault_timeline"]["idempotency"][
            "started_monotonic_ms"
        ] = equal_lifecycle_boundary["ingestion_evidence"][
            "completed_monotonic_ms"
        ]
        _rebind_raw(equal_lifecycle_boundary)
        with self.assertRaisesRegex(ValueError, "lifecycle timeline is invalid"):
            self.report(equal_lifecycle_boundary)

        equal_query_boundary = _passing_observations()
        first_request_start = min(
            sample["started_monotonic_ms"]
            for samples in equal_query_boundary["request_samples"].values()
            for sample in samples
        )
        equal_query_boundary["ingestion_evidence"][
            "query_ready_monotonic_ms"
        ] = first_request_start
        _rebind_raw(equal_query_boundary)
        with self.assertRaisesRegex(ValueError, "precede every measured HTTP"):
            self.report(equal_query_boundary)

        forged_generation = _passing_observations()
        forged_generation["ingestion_evidence"]["active_generations"][
            "load-tenant-03"
        ] = "forged-generation"
        _rebind_raw(forged_generation)
        with self.assertRaisesRegex(ValueError, "active embedding generations"):
            self.report(forged_generation)

        incomplete_embedding_coverage = _passing_observations()
        incomplete_embedding_coverage["ingestion_evidence"][
            "embedding_generation_coverage"
        ]["load-tenant-01"]["covered_chunks"] -= 1
        _rebind_raw(incomplete_embedding_coverage)
        with self.assertRaisesRegex(ValueError, "embedding coverage"):
            self.report(incomplete_embedding_coverage)

        forged_acl_coverage = _passing_observations()
        forged_acl_coverage["ingestion_evidence"]["acl_coverage"][
            "visible_same_tenant_active_chunks"
        ] += 1
        forged_acl_coverage["ingestion_evidence"]["primary_visible_chunks"] += 1
        _rebind_raw(forged_acl_coverage)
        with self.assertRaisesRegex(ValueError, "ACL coverage"):
            self.report(forged_acl_coverage)

        forged_recovery_job = _passing_observations()
        forged_recovery_job["ingestion_evidence"]["recovered_job"][
            "request_fingerprint"
        ] = "sha256:" + "0" * 64
        _rebind_raw(forged_recovery_job)
        with self.assertRaisesRegex(ValueError, "fixed load-v1 operation"):
            self.report(forged_recovery_job)

        invented_recovery_tasks = _passing_observations()
        invented_recovery_tasks["ingestion_evidence"][
            "recovered_job_linked_task_count"
        ] = 50
        _rebind_raw(invented_recovery_tasks)
        with self.assertRaisesRegex(ValueError, "nonexistent durable task nodes"):
            self.report(invented_recovery_tasks)

        forged_idempotency = _passing_observations()
        forged_idempotency["ingestion_evidence"][
            "idempotency_before_state_sha256"
        ] = "sha256:" + "2" * 64
        forged_idempotency["ingestion_evidence"][
            "idempotency_after_state_sha256"
        ] = "sha256:" + "2" * 64
        _rebind_raw(forged_idempotency)
        with self.assertRaisesRegex(ValueError, "does not bind load graph-state"):
            self.report(forged_idempotency)

        forged_pre_generation = _passing_observations()
        for field in ("before_idempotent_replay", "after_idempotent_replay"):
            snapshot = forged_pre_generation["load_graph_state"][field]
            snapshot["label_counts"]["EntityMention"] -= 1
            snapshot["business_node_count"] -= 1
            snapshot["sha256"] = "sha256:" + "6" * 64
        forged_pre_generation["ingestion_evidence"].update(
            {
                "idempotency_before_state_sha256": "sha256:" + "6" * 64,
                "idempotency_after_state_sha256": "sha256:" + "6" * 64,
            }
        )
        _rebind_raw(forged_pre_generation)
        with self.assertRaisesRegex(ValueError, "committed load-v1 graph shape"):
            self.report(forged_pre_generation)

        identical_lifecycle_states = _passing_observations()
        query_ready = deepcopy(
            identical_lifecycle_states["load_graph_state"]["query_ready_state"]
        )
        identical_lifecycle_states["load_graph_state"][
            "before_idempotent_replay"
        ] = deepcopy(query_ready)
        identical_lifecycle_states["load_graph_state"][
            "after_idempotent_replay"
        ] = deepcopy(query_ready)
        identical_lifecycle_states["ingestion_evidence"].update(
            {
                "idempotency_before_state_sha256": query_ready["sha256"],
                "idempotency_after_state_sha256": query_ready["sha256"],
            }
        )
        _rebind_raw(identical_lifecycle_states)
        with self.assertRaisesRegex(ValueError, "committed load-v1 graph shape"):
            self.report(identical_lifecycle_states)

        incomplete_load_shape = _passing_observations()
        snapshot = incomplete_load_shape["load_graph_state"]["query_ready_state"]
        snapshot["label_counts"]["EntityMention"] -= 1
        snapshot["business_node_count"] -= 1
        snapshot["sha256"] = "sha256:" + "6" * 64
        _rebind_raw(incomplete_load_shape)
        with self.assertRaisesRegex(ValueError, "committed load-v1 graph shape"):
            self.report(incomplete_load_shape)

        dropped_load_jobs = _passing_observations()
        for field in ("pre_validation_state", "post_validation_state"):
            snapshot = dropped_load_jobs["canonical_graph"][field]
            snapshot["business_node_count"] -= 480
            snapshot["label_counts"].pop("IngestionJob")
            snapshot["sha256"] = "e" * 64
        dropped_load_jobs["quality_evidence"]["graph_state_sha256"] = (
            "sha256:" + "e" * 64
        )
        _rebind_raw(dropped_load_jobs)
        with self.assertRaisesRegex(ValueError, "preserve.*load-v1 shape"):
            self.report(dropped_load_jobs)

        forged_audit_identity = _passing_observations()
        deletion = forged_audit_identity["deletion_evidence"]
        initial_job = next(
            item
            for item in deletion["durable_audit_jobs"]
            if item["operation"] == "INITIAL_LOAD"
        )
        original_job_id = initial_job["job_id"]
        initial_job["job_id"] = "forged-initial-load-job"
        deletion["durable_audit_job_ids"] = sorted(
            "forged-initial-load-job" if item == original_job_id else item
            for item in deletion["durable_audit_job_ids"]
        )
        _rebind_raw(forged_audit_identity)
        with self.assertRaisesRegex(ValueError, "committed lifecycle operations"):
            self.report(forged_audit_identity)

        forged_tombstone = _passing_observations()
        forged_tombstone["deletion_evidence"][
            "tombstone_deleted_by_job_id"
        ] = "forged-delete-job"
        _rebind_raw(forged_tombstone)
        with self.assertRaisesRegex(ValueError, "tombstone does not bind"):
            self.report(forged_tombstone)

    def test_backup_restore_requires_exact_business_graph_counts(self) -> None:
        for field in (
            "restored_business_node_count",
            "restored_business_relationship_count",
        ):
            with self.subTest(field=field):
                observations = _passing_observations()
                observations["backup_evidence"][field] += 1
                with self.assertRaisesRegex(
                    ValueError, "source and restored business graph counts differ"
                ):
                    self.report(observations)

        changed_label_inventory = _passing_observations()
        changed_label_inventory["canonical_graph"]["restored_state"][
            "label_counts"
        ]["Chunk"] -= 1
        _rebind_raw(changed_label_inventory)
        report = self.report(changed_label_inventory)
        self.assertFalse(report["production_candidate_eligible"])
        self.assertTrue(
            any("restored canonical graph" in item for item in report["failures"])
        )

        unverified_schema = _passing_observations()
        unverified_schema["canonical_graph"]["pre_validation_state"][
            "schema_and_indexes_verified"
        ] = False
        _rebind_raw(unverified_schema)
        with self.assertRaisesRegex(ValueError, "schema and indexes"):
            self.report(unverified_schema)

    def test_limitations_and_residual_risks_are_required(self) -> None:
        for field in ("limitations", "residual_risks"):
            with self.subTest(field=field):
                observations = _passing_observations()
                observations[field] = []
                with self.assertRaisesRegex(ValueError, f"{field} must not be empty"):
                    self.report(observations)

        missing_prerequisite = _passing_observations()
        missing_prerequisite["deployment_prerequisites"].pop()
        with self.assertRaisesRegex(ValueError, "missing required release boundaries"):
            self.report(missing_prerequisite)

    def test_reviewed_stage9_file_digests_are_pinned(self) -> None:
        runner = (ROOT / "scripts" / "run_stage9_validation.sh").read_text(
            encoding="utf-8"
        )
        for module in (
            "build_backup_observation",
            "build_load_corpus",
            "build_production_report",
            "load_production_corpus",
            "run_large_database_quality",
            "run_production_load",
        ):
            with self.subTest(module=module):
                self.assertIn(f"python -m scripts.{module}", runner)
                self.assertNotIn(f"python scripts/{module}.py", runner)
        self.assertIn(
            "--initial-load-transaction-timeout-seconds", runner
        )
        self.assertIn(
            "configured_warmup_requests=$(json_value retrieval.warmup_requests)",
            runner,
        )
        self.assertIn('[ "$configured_warmup_requests" != "64" ]', runner)
        self.assertIn(
            "configured_answer_warmup_requests=$(json_value answer.warmup_requests)",
            runner,
        )
        self.assertIn(
            '[ "$configured_answer_warmup_requests" != "30" ]',
            runner,
        )
        self.assertIn("python -m scripts.run_large_database_quality", runner)
        self.assertIn('--config "$config_path"', runner)
        self.assertIn("resolve_production_answer_retrieval_limits", runner)
        self.assertIn("uv run --locked python - <<'PY'", runner)
        config = _load(
            ROOT / "evaluation" / "production-reference-config.v1.json"
        )
        self.assertEqual(config["version"], "1.0.5")
        self.assertEqual(config["container"]["cpus"], 8)
        self.assertIn('[ "$container_cpus" != "8" ]', runner)
        self.assertIn('[ "$host_cpu_count" -lt "$container_cpus" ]', runner)
        self.assertIn("docker info --format '{{.NCPU}}'", runner)
        self.assertIn('require_docker_cpu_capacity "$container_cpus"', runner)
        self.assertIn(r"$0 ~ /(^|\/)[^\/]+\.egg-info(\/|$)/", runner)
        self.assertLess(
            runner.index('require_docker_cpu_capacity "$container_cpus"'),
            runner.index('echo "Checking committed inputs and deterministic builders"'),
        )
        self.assertLess(
            runner.index('require_docker_cpu_capacity "$container_cpus"'),
            runner.index('docker pull "$image_ref"'),
        )
        workflow = (
            ROOT / ".github" / "workflows" / "stage9-validation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn(
            "docker info --format 'Docker daemon CPUs: {{.NCPU}}'", workflow
        )
        self.assertEqual(config["retrieval"]["warmup_requests"], 64)
        self.assertEqual(config["answer"]["warmup_requests"], 30)
        self.assertEqual(
            config["answer"]["retrieval_limits"],
            asdict(PRODUCTION_ANSWER_RETRIEVAL_LIMITS),
        )
        self.assertEqual(config["neo4j"]["transaction_timeout_seconds"], 300)
        self.assertEqual(
            config["neo4j"]["initial_load_transaction_timeout_seconds"],
            60,
        )
        self.assertEqual(
            config["neo4j"]["online_transaction_timeout_seconds"],
            5,
        )

    def test_stage9_runner_requires_docker_daemon_cpu_capacity(self) -> None:
        runner = (ROOT / "scripts" / "run_stage9_validation.sh").read_text(
            encoding="utf-8"
        )
        function_start = runner.index("validate_docker_cpu_capacity() {")
        function_end = runner.index("\nneo4j_image=", function_start)
        functions = runner[function_start:function_end]
        command = f"""{functions}
docker() {{
  if [ "$1" != "info" ] || [ "$2" != "--format" ] || \\
     [ "$3" != "{{{{.NCPU}}}}" ]; then
    return 97
  fi
  if [ "${{STAGE9_TEST_DOCKER_FAILURE:-0}}" = "1" ]; then
    return 42
  fi
  printf '%s\\n' "${{STAGE9_TEST_DOCKER_NCPU:-}}"
}}
require_docker_cpu_capacity 8
"""

        def run_preflight(value: str, *, docker_failure: bool = False):
            environment = os.environ.copy()
            environment["STAGE9_TEST_DOCKER_NCPU"] = value
            environment["STAGE9_TEST_DOCKER_FAILURE"] = (
                "1" if docker_failure else "0"
            )
            return subprocess.run(
                ["sh", "-c", command],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

        for value in ("8", "12"):
            with self.subTest(value=value):
                completed = run_preflight(value)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(
                    f"Docker daemon CPU capacity: {value} (required: 8)",
                    completed.stdout,
                )

        completed = run_preflight("7")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "Docker daemon exposes 7 CPU(s); production-reference requires at least 8",
            completed.stderr,
        )

        for value in ("", "8.0", "unknown", " 8"):
            with self.subTest(invalid_value=value):
                completed = run_preflight(value)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "Docker daemon returned an invalid .NCPU value",
                    completed.stderr,
                )

        completed = run_preflight("8", docker_failure=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "could not inspect Docker daemon CPU capacity", completed.stderr
        )

    def test_reviewed_stage9_file_digest_drift_is_rejected(self) -> None:
        fields = (
            "answer_embedding_corpus_digest",
            "configuration_digest",
            "contract_digest",
            "load_corpus_digest",
            "profile_digest",
        )
        for field in fields:
            with self.subTest(field=field):
                observations = _passing_observations()
                observations["versions"][field] = "f" * 64
                observations["evidence_manifest"][
                    {
                        "answer_embedding_corpus_digest": "answer_embedding_corpus",
                        "configuration_digest": "production_configuration",
                        "contract_digest": "acceptance_contract",
                        "load_corpus_digest": "load_corpus",
                        "profile_digest": "validation_profile",
                    }[field]
                ]["sha256"] = "f" * 64
                with self.assertRaisesRegex(ValueError, "reviewed Stage 9 input"):
                    self.report(observations)

        semantic_drift = _passing_observations()
        semantic_drift["versions"]["stage8_report_semantic_digest"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "reviewed Stage 9 input"):
            self.report(semantic_drift)

        for field in (
            "api_version",
            "extractor_version",
            "governance_policy_version",
            "graph_schema_version",
            "hardware_profile",
            "index_version",
            "llm_model",
            "llm_provider",
            "llm_revision",
            "neo4j_image",
            "output_schema_version",
            "prompt_version",
            "splitter_version",
            "stage8_gold_version",
        ):
            with self.subTest(component_identity=field):
                drifted = _passing_observations()
                drifted["versions"][field] = "stale"
                with self.assertRaisesRegex(ValueError, "component identity"):
                    self.report(drifted)
        for field in ("neo4j_image_digest", "stage8_gold_digest"):
            with self.subTest(component_digest=field):
                drifted = _passing_observations()
                drifted["versions"][field] = "f" * 64
                with self.assertRaisesRegex(ValueError, "component identity"):
                    self.report(drifted)

    def test_contract_profile_versions_and_actual_repo_digest_cannot_drift(self) -> None:
        weakened_contract = deepcopy(self.contract)
        next(
            item for item in weakened_contract["metrics"] if item["id"] == "mrr"
        )["target"] = 0.1
        with self.assertRaisesRegex(ValueError, "threshold changed"):
            build_production_candidate_report(
                _passing_observations(), weakened_contract, self.profile
            )

        same_version_contract_drift = deepcopy(self.contract)
        same_version_contract_drift["owner"] = "unreviewed-owner"
        with self.assertRaisesRegex(ValueError, "content differs from the reviewed"):
            build_production_candidate_report(
                _passing_observations(), same_version_contract_drift, self.profile
            )

        weakened_profile = deepcopy(self.profile)
        weakened_profile["metric_policy"]["performance_results"] = "informational_only"
        with self.assertRaisesRegex(ValueError, "gate quality and performance"):
            build_production_candidate_report(
                _passing_observations(), self.contract, weakened_profile
            )

        stale = _passing_observations()
        stale["versions"]["contract_version"] = "stale"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.report(stale)

        stale_configuration = _passing_observations()
        stale_configuration["versions"]["configuration_version"] = "stale"
        with self.assertRaisesRegex(ValueError, "configuration_version is stale"):
            self.report(stale_configuration)

        mismatched_repo_digest = _passing_observations()
        mismatched_repo_digest["runtime_environment"][
            "actual_neo4j_repo_digest"
        ] = "f" * 64
        with self.assertRaisesRegex(ValueError, "RepoDigest"):
            self.report(mismatched_repo_digest)


if __name__ == "__main__":
    unittest.main()
