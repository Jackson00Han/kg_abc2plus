"""Production-reference qualification logic for Stage 9.

The module accepts observations as plain mappings and verifies raw quality
cases against pinned committed gold.  Qualification fields are output-only: a
caller may provide measurements, scenario outcomes, version identities, and
disclosed risks, but cannot claim that the run passed or is production-candidate
eligible.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from numbers import Real
import re
from typing import Any, Iterable, Mapping

from graphrag_prod.domain import embedding_index_generation_id, ingestion_job_id

from .gates import compare
from .metrics import nearest_rank_percentile
from .production_config import (
    PRODUCTION_ANSWER_RETRIEVAL_LIMITS,
    PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION,
    PRODUCTION_REFERENCE_CONFIG_VERSION,
    resolve_production_answer_retrieval_limits,
)
from .quality_evidence import (
    QUALITY_CASE_EVIDENCE_SCHEMA_VERSION,
    canonical_quality_digest,
    evaluate_http_answer_commitments,
    evaluate_quality_case_evidence,
)
from .reference_predictions import (
    REFERENCE_PREDICTION_PROVIDER,
    REFERENCE_PREDICTION_SHA256,
    REFERENCE_PREDICTION_VERSION,
)


PRODUCTION_REPORT_SCHEMA_VERSION = "production-candidate-report-v1"
PRODUCTION_OBSERVATION_SCHEMA_VERSION = "production-observations-v1"

_CONTRACT_VERSION = "1.0.1"
_PROFILE_VERSION = "1.0.0"
_PROFILE_ID = "production-reference"

# These byte-level digests pin the exact reviewed Stage 9 inputs. A version
# string alone is not sufficient because an in-place edit could otherwise
# change the acceptance boundary without changing its declared identity.
_REVIEWED_CONTRACT_FILE_SHA256 = (
    "sha256:cd6927fd29436cdd884bd004004989dda8b98df746e52b64057e5d407dc048d3"
)
_REVIEWED_PROFILE_FILE_SHA256 = (
    "sha256:6fad16ee8a99d79779f5fd5bff6c6f5785dd20e71e69853c6d44278a677755e9"
)
_REVIEWED_CONFIGURATION_FILE_SHA256 = (
    "sha256:af3a596880a193208d17d2171dcc6e63eff84fee1b8dc669a8031e62f0fee88e"
)
_REVIEWED_LOAD_MANIFEST_FILE_SHA256 = (
    "sha256:91e05a35a8e074a1a9a4216e2fc6ba7e705bcdad0a4e03bad4157e50054e9afe"
)
_REVIEWED_DEV_CORPUS_MANIFEST_FILE_SHA256 = (
    "sha256:54e73d0e249b7ed28b8b137ad8c87b4fede184647b5a44be23eb1568e8efe666"
)
_REVIEWED_STAGE8_REPORT_SEMANTIC_SHA256 = (
    "sha256:af94664fb502498b884eada4b27af892d13d73b9fcf66790601957e672cb126d"
)
_REVIEWED_STAGE8_GOLD_MANIFEST_FILE_SHA256 = (
    "sha256:def7d0211f52b8fb67d8eb730d26bf72f0dae88bf6a814b64af1a9b25b1c1b7f"
)

_EXPECTED_LOAD_GRAPH_SHAPE = {
    "business_node_count": 73_932,
    "business_relationship_count": 147_365,
    "label_counts": {
        "Chunk": 24_000,
        "ChunkEmbedding": 24_000,
        "Document": 240,
        "DocumentVersion": 480,
        "EmbeddingGeneration_20396121c96b586680139b7e": 500,
        "EmbeddingGeneration_3ebac453136655ee9bab437e": 500,
        "EmbeddingGeneration_6d5c490bc56c5cff9aa9a089": 500,
        "EmbeddingGeneration_b4df8766d71b5957ab8491ac": 500,
        "EmbeddingGeneration_bf694c4ee7a9575884185600": 10_000,
        "EmbeddingIndexGeneration": 5,
        "Entity": 240,
        "EntityMention": 24_000,
        "GraphGovernancePolicy": 1,
        "GraphPipelineProfile": 1,
        "IngestionJob": 480,
        "InitialLoadJob": 480,
        "KnowledgeSnapshot": 480,
        "TenantCorpusState": 5,
    },
}
_EXPECTED_PRE_GENERATION_LOAD_GRAPH_SHAPE = {
    "business_node_count": 73_927,
    "business_relationship_count": 147_360,
    "label_counts": {
        "Chunk": 24_000,
        "ChunkEmbedding": 24_000,
        "Document": 240,
        "DocumentVersion": 480,
        "Entity": 240,
        "EntityMention": 24_000,
        "GraphGovernancePolicy": 1,
        "GraphPipelineProfile": 1,
        "IngestionJob": 480,
        "InitialLoadJob": 480,
        "KnowledgeSnapshot": 480,
        "TenantCorpusState": 5,
    },
}
_REVIEWED_STAGE8_GRAPH_METRICS = {
    "entity_precision": 1.0,
    "entity_resolution_accuracy": 1.0,
    "relationship_precision": 1.0,
}

# Parsed contract/profile inputs are pinned independently from the byte-level
# evidence above so callers cannot supply altered mappings while retaining a
# claim about the reviewed on-disk file digest.
_REVIEWED_CONTRACT_CANONICAL_SHA256 = (
    "40620af624854268112569efa4b941511203a8944777de56912b11cea524c317"
)
_REVIEWED_PROFILE_CANONICAL_SHA256 = (
    "64860eaed196e7fec8a772b3a76cde6b307212aea7bb7c620d65f68639f6ac71"
)

_THRESHOLDS: dict[str, tuple[str, int | float]] = {
    "entity_precision": (">=", 0.95),
    "relationship_precision": (">=", 0.95),
    "entity_resolution_accuracy": (">=", 0.95),
    "recall_at_5": (">=", 0.9),
    "mrr": (">=", 0.8),
    "ndcg_at_5": (">=", 0.85),
    "unauthorized_exposure_count": ("=", 0),
    "supported_claim_rate": (">=", 0.95),
    "citation_precision": (">=", 0.95),
    "citation_coverage": (">=", 0.95),
    "numerical_fidelity": ("=", 1.0),
    "refusal_f1": (">=", 0.9),
    "ingestion_success_rate": (">=", 0.995),
    "idempotency_mismatch_count": ("=", 0),
    "deletion_residue_count": ("=", 0),
    "recovery_success_rate": ("=", 1.0),
    "retrieval_p95_ms": ("<=", 1000),
    "answer_p95_ms": ("<=", 15000),
    "retrieval_throughput_rps": (">=", 8),
    "server_error_rate": ("<=", 0.005),
}

_RATIO_METRICS = frozenset(
    {
        "entity_precision",
        "relationship_precision",
        "entity_resolution_accuracy",
        "recall_at_5",
        "mrr",
        "ndcg_at_5",
        "supported_claim_rate",
        "citation_precision",
        "citation_coverage",
        "numerical_fidelity",
        "refusal_f1",
        "ingestion_success_rate",
        "recovery_success_rate",
        "server_error_rate",
    }
)
_COUNT_METRICS = frozenset(
    {
        "unauthorized_exposure_count",
        "idempotency_mismatch_count",
        "deletion_residue_count",
    }
)

_OBSERVATION_FIELDS = frozenset(
    {
        "canonical_graph",
        "backup_evidence",
        "deletion_evidence",
        "evidence_manifest",
        "fault_timeline",
        "schema_version",
        "profile_id",
        "provider_evidence",
        "quality_evidence",
        "ingestion_evidence",
        "request_samples",
        "runtime_environment",
        "suite_results",
        "workload",
        "metrics",
        "latency_ms",
        "load_graph_state",
        "traffic",
        "cost",
        "scenarios",
        "versions",
        "limitations",
        "residual_risks",
        "deployment_prerequisites",
    }
)
_FORGED_QUALIFICATION_FIELDS = frozenset(
    {
        "passed",
        "failures",
        "production_candidate_eligible",
        "qualification_status",
        "semantic_digest",
    }
)
_WORKLOAD_FIELDS = frozenset(
    {
        "chunk_count",
        "concurrency",
        "sustained_seconds",
        "answer_samples",
        "warmed",
    }
)
_LATENCY_FIELDS = frozenset(
    {
        "ingestion",
        "retrieval_stage_ms",
        "embedding_provider",
        "llm",
    }
)
_TRAFFIC_FIELDS = frozenset(
    {
        "ingestion_chunks",
        "ingestion_started_monotonic_ms",
        "ingestion_completed_monotonic_ms",
    }
)
_COST_FIELDS = frozenset(
    {
        "currency",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "request_cost_usd",
    }
)

_REQUEST_SAMPLE_SECTIONS = frozenset({"answer", "retrieval"})
_ANSWER_SAMPLE_CASE_IDS = frozenset(
    {
        "cross_chunk-boundary-01",
        "cross_chunk-boundary-02",
        "cross_chunk-success-01",
        "cross_chunk-success-02",
        "cross_chunk-success-03",
        "cross_chunk-success-04",
        "exact_value-boundary-01",
        "exact_value-boundary-02",
        "exact_value-success-01",
        "exact_value-success-02",
        "exact_value-success-03",
        "exact_value-success-04",
        "graph_relationship-boundary-01",
        "graph_relationship-boundary-02",
        "graph_relationship-success-01",
        "graph_relationship-success-02",
        "graph_relationship-success-03",
        "graph_relationship-success-04",
        "single_chunk-boundary-01",
        "single_chunk-boundary-02",
        "single_chunk-success-01",
        "single_chunk-success-02",
        "single_chunk-success-03",
        "single_chunk-success-04",
        "temporal_conflict-boundary-01",
        "temporal_conflict-boundary-02",
        "temporal_conflict-success-01",
        "temporal_conflict-success-02",
        "temporal_conflict-success-03",
        "temporal_conflict-success-04",
    }
)
_ANSWER_SAMPLE_EXPECTATIONS_SHA256 = (
    "08c8fbb3ca20d9b0a3717f817ed275c43bcdb9636d39c0101f2d02abd28abd7c"
)
_LOAD_SAMPLE_CASE_IDS = frozenset(
    f"load-anchor-{index:02d}" for index in range(64)
)
_LOAD_SAMPLE_EXPECTATIONS_SHA256 = (
    "0205a5fed773ed0b9850953853a9425111b80b9ab72f259aa49a2b3926fdc4b1"
)
_EXPECTED_RETRIEVAL_CLIENT_IDS = frozenset(
    f"retrieval-{index:02d}" for index in range(8)
)
_MAX_RETRIEVAL_CLIENT_IDLE_MS = 1_000.0
_REQUEST_SAMPLE_FIELDS = frozenset(
    {
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
    }
)

_FAULT_EVENT_FIELDS = frozenset(
    {
        "assertion_failures",
        "completed_monotonic_ms",
        "domain_status",
        "error_code",
        "http_status",
        "reason",
        "started_monotonic_ms",
    }
)
_ACCESS_FAULT_EVENT_FIELDS = _FAULT_EVENT_FIELDS | {"access_evidence"}
_ACCESS_EVIDENCE_FIELDS = frozenset(
    {"dataset_id", "inventory", "principal", "probes", "schema_version"}
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
_ACCESS_TRACE_STAGES = (
    "vector_recall",
    "bm25_recall",
    "seed_ranking",
    "graph_expansion",
    "candidate_vector_ranking",
    "final_ranking",
)
_EXPECTED_ACCESS_INVENTORY = {
    "active": {
        "count": 12_000,
        "chunk_ids_sha256": "sha256:df84ca6b4b85144ece57179df976fc228cd43d21be294aa27d70c3a754083094",
    },
    "all": {
        "count": 24_000,
        "chunk_ids_sha256": "sha256:5e74734f942f1c7f40009cb3837dafa48d9b95560032e9329b9782a0b62ec90e",
    },
    "authorized": {
        "count": 7_500,
        "chunk_ids_sha256": "sha256:37994b794a100c63933a62bc446a51bf4acef4e54ad2407e0b7b361aacf87f0a",
    },
    "forbidden": {
        "count": 4_500,
        "chunk_ids_sha256": "sha256:0d66b860fe1c2cfb3e3d2089d7b0db1364c010a31948fbefb28fad4951bdb3d4",
    },
    "inactive": {
        "count": 12_000,
        "chunk_ids_sha256": "sha256:59385bc9f4ca63ec5046c1d43cc6cef3098472f3fff5988b5916afbdb0e10578",
    },
}
_EXPECTED_ACCESS_PRINCIPAL = {
    "groups": ["load-tenant-01-group-01", "load-tenant-01-public"],
    "tenant_id": "load-tenant-01",
}
_EXPECTED_ACCESS_PROBES = {
    "same-tenant-denied-00": {
        "canary_chunk_id": "4a9f0654-7c21-5660-8e65-c56bd18b0a65",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "same-tenant-denied",
        "markers": frozenset(
            {
                "4a9f0654-7c21-5660-8e65-c56bd18b0a65",
                "12cca330-ea5c-5b63-987d-6d8a701d2c41",
                "load-tenant-01:document-004:v2:chunk-000",
                "load-tenant-01-group-02",
            }
        ),
        "query_text_sha256": "sha256:1e1d717d451e611d93da3004b61fbc4271153d1f24472927a20d582ec5930f81",
        "query_vector_checksum": "sha256:7e8b73320a586c87f267b5475e8f02ac85e3ab04ab068402e770c046cfc119c2",
        "source_text_sha256": "sha256:01160828eb25ff99434c3d82ed5692993fa96d960d02b1225c8fd699e0ccd9ac",
        "target_tenant_id": "load-tenant-01",
        "version_id": "66b9b612-3e24-59b8-806e-be7cdb107f0f",
    },
    "same-tenant-denied-01": {
        "canary_chunk_id": "69a8b94f-93d9-5d9e-a8bf-b1c2518d4353",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "same-tenant-denied",
        "markers": frozenset(
            {
                "69a8b94f-93d9-5d9e-a8bf-b1c2518d4353",
                "db2c99a7-e1f6-59b5-ba4d-1a1b44b85fa5",
                "load-tenant-01:document-006:v2:chunk-000",
                "load-tenant-01-group-03",
            }
        ),
        "query_text_sha256": "sha256:86da0c1878432a4901ef47daab958f4c06ce1919aae29496b45924e54044d584",
        "query_vector_checksum": "sha256:fa798104326b1cc07e084b11da82da7baf3b7fcfdb544264a80dc193164c3a86",
        "source_text_sha256": "sha256:0f29a1b592b893234fd3664d772acd88f6116bd315e9764573b0b4a8dc9838c5",
        "target_tenant_id": "load-tenant-01",
        "version_id": "10777a7b-bcd7-51b0-a203-c6c7ecdceb7d",
    },
    "same-tenant-denied-02": {
        "canary_chunk_id": "377cee00-aabf-56a7-aa11-280e9bb21010",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "same-tenant-denied",
        "markers": frozenset(
            {
                "377cee00-aabf-56a7-aa11-280e9bb21010",
                "9f06f221-fb47-50ad-8775-640d0d1d8508",
                "load-tenant-01:document-012:v2:chunk-000",
                "load-tenant-01-group-02",
            }
        ),
        "query_text_sha256": "sha256:92624ea33f9be3878ecbfc284cb8403e492ae7cfcebe60fa6a4cd7d2c776c667",
        "query_vector_checksum": "sha256:4f1495f87fc36053455fe61db85f8b84edd8f14d42ff642a92efe3a3b72e871d",
        "source_text_sha256": "sha256:cf781a50160e87fe18016d97b2f77e39b35caae44916153199eaccb968f9cf41",
        "target_tenant_id": "load-tenant-01",
        "version_id": "d90114d3-d9d9-5b56-b2d5-4c7b1aacfab6",
    },
    "same-tenant-denied-03": {
        "canary_chunk_id": "ad1c7918-ccbf-5e39-9c84-83d3f081ee14",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "same-tenant-denied",
        "markers": frozenset(
            {
                "ad1c7918-ccbf-5e39-9c84-83d3f081ee14",
                "8156ba0e-d4b1-53eb-a038-21ea121670e3",
                "load-tenant-01:document-014:v2:chunk-000",
                "load-tenant-01-group-03",
            }
        ),
        "query_text_sha256": "sha256:b2b52b14f0a6d0d76c6895cfc27c27da073717cac33bf35a11d6950fcc4a5b24",
        "query_vector_checksum": "sha256:77f464a9c2740ea4535d8cf7e980f6e36010584dfba4b72a836f236f2d2f3d8c",
        "source_text_sha256": "sha256:9a3fab04661bfa78115751ec17d8f01fd736a0b9ac8f22b5851cc7dd13dbfe3b",
        "target_tenant_id": "load-tenant-01",
        "version_id": "931bebce-7a51-57ff-bfad-b3c7c98b388c",
    },
    "cross-tenant-denied-00": {
        "canary_chunk_id": "1b03f799-bf35-5e2d-af97-4f0e54cd6940",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "cross-tenant-denied",
        "markers": frozenset(
            {
                "1b03f799-bf35-5e2d-af97-4f0e54cd6940",
                "c2af22e6-662b-51ed-a0c4-6637353f9509",
                "load-tenant-02:document-001:v2:chunk-000",
                "load-tenant-02-public",
                "load-tenant-02",
            }
        ),
        "query_text_sha256": "sha256:b5807f65f854bf453ee0f982876946a53880a56e34ff433b0b6d89d29c7ee3a7",
        "query_vector_checksum": "sha256:66a11dd1f2c68a0c06a835c4ab0aea4d85fc68358130211434a6de25398dc4d8",
        "source_text_sha256": "sha256:cf1f433d61cd12dbf081bec93bfb434629fae1866d49859f10e795cad5f2ce19",
        "target_tenant_id": "load-tenant-02",
        "version_id": "fedb324b-4857-557c-afce-2179c0136168",
    },
    "cross-tenant-denied-01": {
        "canary_chunk_id": "018abbae-5caa-59cc-957b-4562974988d9",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "cross-tenant-denied",
        "markers": frozenset(
            {
                "018abbae-5caa-59cc-957b-4562974988d9",
                "23e07b54-ad0b-5516-b332-85ca8dac0c21",
                "load-tenant-03:document-001:v2:chunk-000",
                "load-tenant-03-public",
                "load-tenant-03",
            }
        ),
        "query_text_sha256": "sha256:72468d230f0b046d757d957bdc8ff667b3a5fa482f0a933a0c3ca276e6a7d583",
        "query_vector_checksum": "sha256:cc626c8bbd5ca835eca8c65cf9736ac1e2cb970c690bfb2abbdc7e19f4e3a01c",
        "source_text_sha256": "sha256:042fed7c9070158e0e41520dd5108c17428154c5063a5073cf2b380bff4a2d13",
        "target_tenant_id": "load-tenant-03",
        "version_id": "6fbadd79-d5d3-5f0a-9db6-440fc17bdf30",
    },
    "cross-tenant-denied-02": {
        "canary_chunk_id": "49852c99-8317-5209-9470-36bbbda38a30",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "cross-tenant-denied",
        "markers": frozenset(
            {
                "49852c99-8317-5209-9470-36bbbda38a30",
                "fbd375b1-6f1e-5b42-adb4-77a774e4fb48",
                "load-tenant-04:document-001:v2:chunk-000",
                "load-tenant-04-public",
                "load-tenant-04",
            }
        ),
        "query_text_sha256": "sha256:e4a18f848bedb3c917ed91acebc1be1c59ff93fb89003cda8cf4c12ca3582fe0",
        "query_vector_checksum": "sha256:f76cd52400903f363bcb207213e5455b76e88d054c7f91b16b3eeb27ff279e7f",
        "source_text_sha256": "sha256:d3d94316b58a3de91ccb9358db70df0908670a95648f10ddec43cea6e24b196a",
        "target_tenant_id": "load-tenant-04",
        "version_id": "0fdd8a36-0eb0-5b0c-a48e-f76ec961fcac",
    },
    "cross-tenant-denied-03": {
        "canary_chunk_id": "64c14c8b-8964-54f8-908e-f1e74beb41cb",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
        "kind": "cross-tenant-denied",
        "markers": frozenset(
            {
                "64c14c8b-8964-54f8-908e-f1e74beb41cb",
                "42f48789-9235-52d8-8a52-5716aa5aaae0",
                "load-tenant-05:document-001:v2:chunk-000",
                "load-tenant-05-public",
                "load-tenant-05",
            }
        ),
        "query_text_sha256": "sha256:a010a92cf5b25b58ac163065090c2b32ffc4267e561c31b4dff652d4cc23c088",
        "query_vector_checksum": "sha256:a9a88ab3163aab42d2e3d65f1e0ec373047d5c03d1552122563bea30cdb53d7d",
        "source_text_sha256": "sha256:3779cf2901a56e8ee5dc8e36156696d2f5eb1c820f14363e28a6276fddef981d",
        "target_tenant_id": "load-tenant-05",
        "version_id": "64be59f3-fdb9-5cb9-b029-2be9e51a838b",
    },
}

_SUITE_IDS = frozenset(
    {"functional", "performance", "quality", "recovery", "security"}
)
_SUITE_RESULT_FIELDS = frozenset(
    {
        "error_test_ids",
        "failed_test_ids",
        "passed_test_ids",
        "skipped_test_ids",
        "tests_run",
    }
)

_CANONICAL_GRAPH_FIELDS = frozenset(
    {
        "backup_source_state",
        "post_validation_state",
        "pre_validation_state",
        "restored_state",
    }
)
_CANONICAL_GRAPH_STATE_FIELDS = frozenset(
    {
        "business_node_count",
        "business_relationship_count",
        "label_counts",
        "schema_and_indexes_verified",
        "sha256",
    }
)
_RUNTIME_ENVIRONMENT_FIELDS = frozenset(
    {
        "actual_memory_bytes",
        "actual_memory_swap_bytes",
        "actual_nano_cpus",
        "actual_neo4j_image",
        "actual_neo4j_image_id",
        "actual_neo4j_repo_digest",
        "api_process_resource_limit",
        "code_commit",
        "configured_heap_initial",
        "configured_heap_max",
        "configured_pagecache",
        "configured_transaction_timeout",
        "database_initial_node_count",
        "database_initial_relationship_count",
        "host_cpu_count",
        "host_memory_bytes",
        "host_platform",
        "readiness_probe_status",
        "readiness_transaction_timeout_seconds",
        "retrieval_transaction_timeout_seconds",
    }
)
_PROVIDER_EVIDENCE_FIELDS = frozenset(
    {
        "answer_preflight_case_ids",
        "answer_warmup_model_calls",
        "measured_answer_model_calls",
        "measured_embedding_model_calls",
        "mode",
        "peak_concurrency",
    }
)
_PROVIDER_EVIDENCE_MODES = frozenset({"deterministic_reference"})
_EXPECTED_ANSWER_WARMUP_MODEL_CALLS = 30

_EVIDENCE_FIELDS = frozenset({"path", "record_count", "schema", "sha256"})
_EVIDENCE_SCHEMAS = {
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
_EVIDENCE_IDS = frozenset(_EVIDENCE_SCHEMAS)

_QUALITY_EVIDENCE_FIELDS = frozenset(
    {
        "active_generation_ids",
        "answer_retrieval_limits",
        "answer_metrics",
        "case_count",
        "case_evidence",
        "case_ids",
        "case_set_sha256",
        "corpus_version",
        "failures",
        "gold_projection_sha256",
        "graph_state_sha256",
        "passed",
        "prediction_provider",
        "prediction_sha256",
        "prediction_version",
        "production_configuration_sha256",
        "retrieval_metrics",
        "schema_version",
    }
)
_RETRIEVAL_QUALITY_FIELDS = frozenset(
    {
        "answerable_count",
        "evidence_recall_at_5",
        "item_count",
        "mrr",
        "ndcg_at_5",
        "recall_at_5",
        "unauthorized_exposure_count",
    }
)
_ANSWER_QUALITY_FIELDS = frozenset(
    {
        "answer_correctness",
        "citation_attachment_count",
        "citation_coverage",
        "citation_precision",
        "conflict_handling_rate",
        "exact_token_count",
        "expected_conflict_count",
        "expected_refusal_count",
        "expected_temporal_comparison_count",
        "forbidden_answer_exposure_count",
        "generation_failure_count",
        "item_count",
        "material_claim_count",
        "numerical_fidelity",
        "refusal_f1",
        "refusal_precision",
        "refusal_recall",
        "supported_claim_rate",
        "temporal_comparison_rate",
    }
)
_GOLD_CASE_SET_SHA256 = (
    "f966b1012d68fbacfe6ac0ed7b389ea92bd8de259a13c5184a88e62e09cc7829"
)

_INGESTION_EVIDENCE_FIELDS = frozenset(
    {
        "acl_coverage",
        "active_generations",
        "clean_start",
        "completed_monotonic_ms",
        "completed_versions",
        "database_documents",
        "database_versions",
        "embedding_generation_coverage",
        "failed_versions",
        "idempotency_after_state_sha256",
        "idempotency_before_state_sha256",
        "idempotency_mismatch_count",
        "initial_load_transaction_timeout_seconds",
        "interrupted_after_state_sha256",
        "interrupted_before_state_sha256",
        "interrupted_job_count",
        "interrupted_task_node_count",
        "primary_tenant_active_chunks",
        "primary_visible_chunks",
        "recovered_job",
        "recovered_job_id",
        "recovered_job_linked_task_count",
        "recovered_job_task_node_count",
        "recovery_checkpoint",
        "recovery_task_tracking_mode",
        "replayed_active_versions",
        "replay_completed_monotonic_ms",
        "replay_started_monotonic_ms",
        "query_ready_monotonic_ms",
        "schema_version",
        "started_monotonic_ms",
        "submitted_chunks",
        "total_active_chunks",
        "total_historical_chunks",
        "total_versions",
    }
)
_LOAD_ACL_COVERAGE_FIELDS = frozenset(
    {
        "access_groups",
        "cross_tenant_active_chunks",
        "cross_tenant_active_embeddings",
        "denied_same_tenant_active_chunks",
        "denied_same_tenant_active_embeddings",
        "tenant_id",
        "total_same_tenant_active_chunks",
        "total_same_tenant_active_embeddings",
        "visible_same_tenant_active_chunks",
        "visible_same_tenant_active_embeddings",
    }
)
_EMBEDDING_COVERAGE_FIELDS = frozenset(
    {"covered_chunks", "generation_id", "total_chunks"}
)
_RECOVERED_JOB_FIELDS = frozenset(
    {
        "attempts",
        "built_chunk_count",
        "built_embedding_count",
        "built_snapshot_id",
        "completed_tasks",
        "corpus_revision",
        "document_id",
        "expected_active_snapshot_id",
        "expected_tasks",
        "idempotency_key",
        "job_id",
        "max_attempts",
        "operation",
        "operation_key",
        "outcome",
        "phase",
        "request_fingerprint",
        "snapshot_expected_chunk_count",
        "snapshot_manifest_hash",
        "source_generation",
        "status",
        "target_snapshot_id",
        "target_version_id",
        "tenant_id",
    }
)
_DELETION_EVIDENCE_FIELDS = frozenset(
    {
        "deletion_residue_count",
        "delete_job_id",
        "document_id",
        "durable_audit_job_count",
        "durable_audit_job_ids",
        "durable_audit_jobs",
        "durable_audit_records_retained",
        "expected_removed_counts",
        "observed_removed_counts",
        "other_tenant_preserved",
        "preserved_tenant_ids",
        "residue_by_label",
        "schema_version",
        "tenant_id",
        "tombstone_generation",
        "tombstone_deleted_by_job_id",
        "target_active_chunk_ids",
        "target_active_snapshot_id",
        "target_active_version_id",
    }
)
_AUDIT_JOB_FIELDS = frozenset(
    {
        "completed_tasks",
        "document_id",
        "expected_tasks",
        "job_id",
        "operation",
        "operation_key",
        "outcome",
        "phase",
        "status",
        "target_snapshot_id",
        "target_version_id",
        "tenant_id",
    }
)
_LOAD_GRAPH_STATE_FIELDS = frozenset(
    {
        "after_idempotent_replay",
        "before_idempotent_replay",
        "idempotency_mismatch_count",
        "query_ready_state",
        "schema_version",
    }
)
_GRAPH_STATE_SNAPSHOT_FIELDS = frozenset(
    {"business_node_count", "business_relationship_count", "label_counts", "sha256"}
)
_LOAD_TENANTS = (
    "load-tenant-01",
    "load-tenant-02",
    "load-tenant-03",
    "load-tenant-04",
    "load-tenant-05",
)
_EXPECTED_LOAD_ACTIVE_GENERATIONS = {
    "load-tenant-01": "bf694c4e-e7a9-5758-8418-56000e0b8774",
    "load-tenant-02": "b4df8766-d71b-5957-ab84-91ac606e9526",
    "load-tenant-03": "20396121-c96b-5866-8013-9b7ea45b8b12",
    "load-tenant-04": "6d5c490b-c56c-5cff-9aa9-a08908acb7cb",
    "load-tenant-05": "3ebac453-1366-55ee-9bab-437ed375007d",
}
_EXPECTED_LOAD_EMBEDDING_COVERAGE = {
    tenant_id: {
        "covered_chunks": 10_000 if tenant_id == "load-tenant-01" else 500,
        "generation_id": _EXPECTED_LOAD_ACTIVE_GENERATIONS[tenant_id],
        "total_chunks": 10_000 if tenant_id == "load-tenant-01" else 500,
    }
    for tenant_id in _LOAD_TENANTS
}
_EXPECTED_LOAD_ACL_COVERAGE = {
    "access_groups": ["load-tenant-01-group-01", "load-tenant-01-public"],
    "cross_tenant_active_chunks": 2_000,
    "cross_tenant_active_embeddings": 2_000,
    "denied_same_tenant_active_chunks": 2_500,
    "denied_same_tenant_active_embeddings": 2_500,
    "tenant_id": "load-tenant-01",
    "total_same_tenant_active_chunks": 10_000,
    "total_same_tenant_active_embeddings": 10_000,
    "visible_same_tenant_active_chunks": 7_500,
    "visible_same_tenant_active_embeddings": 7_500,
}
_EXPECTED_RECOVERED_INITIAL_LOAD_JOB = {
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
    "request_fingerprint": (
        "sha256:b5b2fac22d28a211d2bfef3b83823061f73f66c9189c2e9e5c5f53d219c9f1a9"
    ),
    "snapshot_expected_chunk_count": 50,
    "snapshot_manifest_hash": (
        "sha256:76d51d17de0bc312e0652a45ce16e706e7401bdc4fe3d6373ef2d89b65e99049"
    ),
    "source_generation": 0,
    "status": "SUCCEEDED",
    "target_snapshot_id": "a7c34cb5-b8eb-5edd-bdbb-441fea1ed6ce",
    "target_version_id": "40fc0422-cf7d-56f6-8557-c1bebb6462f8",
    "tenant_id": "load-tenant-02",
}
_DELETION_LABELS = frozenset(
    {
        "Assertion",
        "Chunk",
        "ChunkEmbedding",
        "Document",
        "DocumentVersion",
        "Entity",
        "EntityMention",
        "GraphGovernanceFinding",
        "KnowledgeSnapshot",
    }
)
_EXPECTED_DELETION_COUNTS = {
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
_DELETION_CANDIDATE = {
    "active_chunk_ids": [
        "1a6ca0b8-67ba-5181-b0fc-55b4d6ce3e79",
        "7700d74f-1ba6-5160-846f-7ec1f2faa5dc",
    ],
    "active_snapshot_id": "0ef4377c-bf62-5c14-9a70-6d024cc141c5",
    "active_version_id": "1236f688-d79b-5120-ad60-fd8f82610388",
    "document_id": "de30ee12-9343-5b6a-a626-9ee6d80754a2",
    "tenant_id": "load-tenant-05",
}
_DELETION_OPERATION_KEY = "stage9-production-delete-validation"
_INITIAL_LOAD_AUDIT_TARGETS = (
    {
        "operation_key": "load-v1:load-tenant-05:document-010:v1",
        "outcome": "CREATED",
        "snapshot_id": "93800a47-d3dd-5029-ac2e-3a300f78a069",
        "version_id": "d06370ca-d6a6-5ef0-973c-bfb5cf891144",
    },
    {
        "operation_key": "load-v1:load-tenant-05:document-010:v2",
        "outcome": "UPDATED",
        "snapshot_id": "0ef4377c-bf62-5c14-9a70-6d024cc141c5",
        "version_id": "1236f688-d79b-5120-ad60-fd8f82610388",
    },
)
_PRESERVED_LOAD_TENANTS = frozenset(
    {"load-tenant-01", "load-tenant-02", "load-tenant-03", "load-tenant-04"}
)
_BACKUP_EVIDENCE_FIELDS = frozenset(
    {
        "backup_sha256",
        "backup_size_bytes",
        "container_resources_match",
        "database",
        "dump_command",
        "load_command",
        "restored_business_node_count",
        "restored_business_relationship_count",
        "restored_container_resource_sha256",
        "restored_state_sha256",
        "schema_and_indexes_verified",
        "schema_version",
        "source_business_node_count",
        "source_business_relationship_count",
        "source_container_resource_sha256",
        "source_state_sha256",
    }
)

_LIFECYCLE_SCENARIOS = (
    "idempotency",
    "interrupted_ingestion",
    "deletion",
    "access_isolation",
    "backup_restore",
)
_DEPENDENCIES = ("neo4j", "embedding_provider", "llm")
_DEPENDENCY_MODES = ("success", "timeout", "unavailable", "failure")
_SCENARIO_IDS = frozenset(
    _LIFECYCLE_SCENARIOS
    + tuple(
        f"{dependency}_{mode}"
        for dependency in _DEPENDENCIES
        for mode in _DEPENDENCY_MODES
    )
)
_PROVIDER_TIMEOUT_SCENARIO_IDS = frozenset(
    {"embedding_provider_timeout", "llm_timeout"}
)
_PROVIDER_TIMEOUT_MIN_MS = 4_900.0
_PROVIDER_TIMEOUT_MAX_MS = 6_000.0
_SCENARIO_FIELDS = frozenset(
    {
        "domain_status",
        "error_code",
        "http_status",
        "latency_ms",
        "passed",
        "reason",
    }
)

_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unauthenticated",
        "forbidden",
        "not_found",
        "conflict",
        "rate_limited",
        "dependency_timeout",
        "dependency_unavailable",
        "overloaded",
        "runtime_closed",
        "internal_error",
    }
)

_VERSION_FIELDS = frozenset(
    {
        "answer_embedding_corpus_digest",
        "answer_embedding_corpus_version",
        "answer_embedding_model",
        "answer_embedding_provider",
        "answer_embedding_revision",
        "answer_embedding_space_id",
        "answer_prediction_digest",
        "answer_prediction_provider",
        "answer_prediction_version",
        "configuration_digest",
        "contract_version",
        "contract_digest",
        "profile_version",
        "profile_digest",
        "load_corpus_id",
        "load_corpus_version",
        "load_corpus_digest",
        "stage8_gold_version",
        "stage8_gold_digest",
        "stage8_report_digest",
        "stage8_report_semantic_digest",
        "api_version",
        "graph_schema_version",
        "governance_policy_version",
        "splitter_version",
        "extractor_version",
        "prompt_version",
        "output_schema_version",
        "embedding_provider",
        "embedding_model",
        "embedding_revision",
        "embedding_space_id",
        "llm_provider",
        "llm_model",
        "llm_revision",
        "index_version",
        "configuration_version",
        "neo4j_image",
        "neo4j_image_digest",
        "python_version",
        "hardware_profile",
        "code_commit",
    }
)
_DIGEST_FIELDS = frozenset(
    {
        "answer_embedding_corpus_digest",
        "answer_prediction_digest",
        "configuration_digest",
        "contract_digest",
        "load_corpus_digest",
        "neo4j_image_digest",
        "profile_digest",
        "stage8_gold_digest",
        "stage8_report_digest",
        "stage8_report_semantic_digest",
    }
)
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_RELATIVE_PATH_PART = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")

_DETERMINISTIC_PROVIDER_LIMITATION = (
    "Provider evidence uses the deterministic reference envelope and does not "
    "establish external embedding or LLM availability, latency, quality, or cost."
)
_EXTERNAL_PROVIDER_PREREQUISITE = (
    "Validate the selected external embedding and LLM providers in the deployment "
    "environment before release."
)
REQUIRED_DEPLOYMENT_PREREQUISITES = (
    "Configure deployment secrets and workload identity in managed secret and "
    "identity systems with least privilege and transport encryption.",
    "Evaluate representative customer corpora with independent relevance, answer, "
    "citation, conflict, refusal, and access-control adjudication.",
    "Exercise monitored backup and restore on the deployment storage class.",
    "Retain qualifying database dumps in an access-controlled evidence store.",
    "Repeat capacity validation on the selected production hardware and topology.",
    "Validate cluster failover and regional recovery against accepted RPO and RTO "
    "targets.",
    "Deploy shared authorization, tenant isolation, and distributed rate-limit "
    "controls for every service replica.",
    "Configure SLO dashboards, alerts, audit retention, incident runbooks, and "
    "on-call ownership.",
    "Complete retention, privacy, data-residency, and applicable compliance review "
    "before release.",
)

_CONTRACT_FIELDS = frozenset(
    {
        "contract_version",
        "target_milestone",
        "owner",
        "scope",
        "datasets",
        "question_classes",
        "metrics",
    }
)
_METRIC_DEFINITION_FIELDS = frozenset(
    {"id", "area", "operator", "target", "unit", "method", "dataset", "dataset_owner"}
)
_PROFILE_FIELDS = frozenset(
    {
        "profile_version",
        "profile_id",
        "base_contract_version",
        "purpose",
        "production_candidate_eligible",
        "overrides",
        "execution",
        "metric_policy",
    }
)


def _exact_mapping(value: Any, fields: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    missing = sorted(fields - set(value))
    extra = sorted(repr(field) for field in set(value) - fields)
    if missing or extra:
        raise ValueError(f"{name} fields are invalid: missing={missing}, extra={extra}")
    return value


def _finite(value: Any, name: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ValueError(f"{name} must be a finite non-negative number")
    return 0.0 if number == 0.0 else number


def _count(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    minimum = 1 if positive else 0
    if value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} integer")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be non-empty text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized)
    ):
        raise ValueError(f"{name} must be safe non-empty text")
    return normalized


def _digest(value: Any, name: str) -> str:
    text = _text(value, name)
    match = _SHA256.fullmatch(text)
    if match is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return f"sha256:{match.group(1).lower()}"


def _notes(value: Any, name: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    normalized = [_text(item, f"{name} item") for item in value]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} entries must be unique")
    if required and not normalized:
        raise ValueError(f"{name} must not be empty")
    return sorted(normalized)


def _relative_path(value: Any, name: str) -> str:
    path = _text(value, name)
    parts = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in parts)
        or any(_RELATIVE_PATH_PART.fullmatch(part) is None for part in parts)
    ):
        raise ValueError(f"{name} must be a normalized relative path")
    return path


def _reject_nested_eligibility(value: Any, path: str = "observations") -> None:
    """Prevent caller-supplied eligibility claims at any nesting depth."""

    if isinstance(value, Mapping):
        forbidden = {"production_candidate_eligible", "qualification_status"}
        found = sorted(repr(field) for field in set(value) & forbidden)
        if found:
            raise ValueError(f"eligibility fields are output-only at {path}: {found}")
        for key, child in value.items():
            _reject_nested_eligibility(child, f"{path}.{key!s}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nested_eligibility(child, f"{path}[{index}]")


def _validate_contract(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    contract = _exact_mapping(contract, _CONTRACT_FIELDS, "acceptance contract")
    if contract.get("contract_version") != _CONTRACT_VERSION:
        raise ValueError("acceptance contract version is not the Stage 1 version")
    if contract.get("target_milestone") != "validation_complete":
        raise ValueError("acceptance contract milestone is invalid")
    _text(contract.get("owner"), "acceptance contract owner")

    scope = contract.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("acceptance contract scope is invalid")
    if scope.get("minimum_validation_chunks") != 10_000:
        raise ValueError("acceptance contract validation scale is not 10,000 Chunks")
    if scope.get("retrieval_concurrency") != 8:
        raise ValueError("acceptance contract retrieval concurrency is not eight")

    datasets = contract.get("datasets")
    if not isinstance(datasets, list):
        raise ValueError("acceptance contract datasets are invalid")
    dataset_by_id: dict[str, Mapping[str, Any]] = {}
    for index, dataset in enumerate(datasets):
        if not isinstance(dataset, Mapping):
            raise ValueError(f"acceptance dataset {index} must be an object")
        dataset_id = dataset.get("id")
        if not isinstance(dataset_id, str) or dataset_id in dataset_by_id:
            raise ValueError("acceptance dataset IDs must be unique text")
        dataset_by_id[dataset_id] = dataset
    if set(dataset_by_id) != {"gold-v1", "graph-review-v1", "load-v1"}:
        raise ValueError("acceptance contract dataset inventory is invalid")
    expected_dataset_minimums = {
        "gold-v1": 49,
        "graph-review-v1": 50,
        "load-v1": 10_000,
    }
    if any(
        dataset_by_id[dataset_id].get("minimum_items") != minimum
        for dataset_id, minimum in expected_dataset_minimums.items()
    ):
        raise ValueError("acceptance contract dataset minimums changed")

    definitions = contract.get("metrics")
    if not isinstance(definitions, list):
        raise ValueError("acceptance contract metrics are invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, definition in enumerate(definitions):
        definition = _exact_mapping(
            definition,
            _METRIC_DEFINITION_FIELDS,
            f"acceptance metric {index}",
        )
        metric_id = definition.get("id")
        if not isinstance(metric_id, str) or metric_id in by_id:
            raise ValueError("acceptance metric IDs must be unique text")
        by_id[metric_id] = definition
    if set(by_id) != set(_THRESHOLDS):
        raise ValueError("acceptance contract must contain exactly the Stage 1 metrics")
    for metric_id, (operator, target) in _THRESHOLDS.items():
        definition = by_id[metric_id]
        observed_target = _finite(
            definition.get("target"),
            f"acceptance metric {metric_id} target",
        )
        if definition.get("operator") != operator or observed_target != float(target):
            raise ValueError(f"acceptance metric {metric_id} threshold changed")
        for field in ("area", "unit", "method", "dataset", "dataset_owner"):
            _text(definition.get(field), f"acceptance metric {metric_id} {field}")
    if _canonical_digest(contract) != _REVIEWED_CONTRACT_CANONICAL_SHA256:
        raise ValueError("acceptance contract content differs from the reviewed file")
    return by_id


def _validate_profile(profile: Mapping[str, Any]) -> None:
    profile = _exact_mapping(profile, _PROFILE_FIELDS, "validation profile")
    if (
        profile.get("profile_version") != _PROFILE_VERSION
        or profile.get("profile_id") != _PROFILE_ID
        or profile.get("base_contract_version") != _CONTRACT_VERSION
        or profile.get("purpose") != "production_candidate_validation"
        or profile.get("production_candidate_eligible") is not True
    ):
        raise ValueError("production-reference profile identity is invalid")
    overrides = _exact_mapping(
        profile.get("overrides"),
        frozenset({"scope", "datasets", "question_classes"}),
        "production-reference overrides",
    )
    if any(overrides[field] != {} for field in overrides):
        raise ValueError("production-reference profile must not override the contract")
    execution = _exact_mapping(
        profile.get("execution"),
        frozenset({"answer_latency_samples", "sustained_load_seconds", "neo4j"}),
        "production-reference execution",
    )
    if (
        execution.get("answer_latency_samples") != 30
        or execution.get("sustained_load_seconds") != 300
        or execution.get("neo4j") != {"mode": "deployment_sized"}
    ):
        raise ValueError("production-reference execution settings changed")
    policy = _exact_mapping(
        profile.get("metric_policy"),
        frozenset({"thresholds", "quality_results", "performance_results"}),
        "production-reference metric policy",
    )
    if policy != {
        "thresholds": "inherited_unchanged",
        "quality_results": "gate",
        "performance_results": "gate",
    }:
        raise ValueError("production-reference must gate quality and performance")
    if _canonical_digest(profile) != _REVIEWED_PROFILE_CANONICAL_SHA256:
        raise ValueError("validation profile content differs from the reviewed file")


def _validate_workload(value: Any) -> dict[str, int | float | bool]:
    workload = _exact_mapping(value, _WORKLOAD_FIELDS, "production workload")
    chunk_count = _count(workload["chunk_count"], "chunk_count", positive=True)
    concurrency = _count(workload["concurrency"], "concurrency", positive=True)
    sustained = _finite(workload["sustained_seconds"], "sustained_seconds")
    answer_samples = _count(
        workload["answer_samples"], "answer_samples", positive=True
    )
    if chunk_count < 10_000:
        raise ValueError("production-reference requires at least 10,000 Chunks")
    if concurrency != 8:
        raise ValueError("production-reference requires exactly eight retrieval clients")
    if sustained < 300:
        raise ValueError("production-reference requires at least 300 sustained seconds")
    if answer_samples < 30:
        raise ValueError("production-reference requires at least 30 answer samples")
    if workload["warmed"] is not True:
        raise ValueError("production-reference measurements must use warmed indexes")
    return {
        "answer_samples": answer_samples,
        "chunk_count": chunk_count,
        "concurrency": concurrency,
        "sustained_seconds": sustained,
        "warmed": True,
    }


def _validate_traffic(
    value: Any,
    workload: Mapping[str, int | float | bool],
) -> tuple[dict[str, int | float], float]:
    traffic = _exact_mapping(value, _TRAFFIC_FIELDS, "production traffic")
    ingestion_chunks = _count(
        traffic["ingestion_chunks"], "ingestion_chunks", positive=True
    )
    if ingestion_chunks < int(workload["chunk_count"]):
        raise ValueError("ingestion throughput must cover the validation corpus")
    started = _finite(
        traffic["ingestion_started_monotonic_ms"],
        "ingestion_started_monotonic_ms",
    )
    completed = _finite(
        traffic["ingestion_completed_monotonic_ms"],
        "ingestion_completed_monotonic_ms",
    )
    if completed <= started:
        raise ValueError("ingestion timeline must have positive duration")
    ingestion_seconds = (completed - started) / 1_000
    ingestion_rps = ingestion_chunks / ingestion_seconds
    return (
        {
            "ingestion_chunks": ingestion_chunks,
            "ingestion_completed_monotonic_ms": completed,
            "ingestion_seconds": ingestion_seconds,
            "ingestion_started_monotonic_ms": started,
        },
        ingestion_rps,
    )


def _status_bucket(status_code: int) -> str:
    if 200 <= status_code <= 299:
        return "2xx"
    if 400 <= status_code <= 499:
        return "4xx"
    if 500 <= status_code <= 599:
        return "5xx"
    raise ValueError("request status_code must be a 2xx, 4xx, or 5xx response")


def _validate_retrieval_case_rotation(samples: list[dict[str, Any]]) -> None:
    by_client: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_client.setdefault(str(sample["client_id"]), []).append(sample)
    if set(by_client) != _EXPECTED_RETRIEVAL_CLIENT_IDS:
        raise ValueError("retrieval samples must use the exact eight client identities")
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


def _retrieval_concurrency_diagnostics(
    samples: list[dict[str, Any]],
    *,
    concurrency: int,
    declared_duration_seconds: float,
) -> dict[str, int | float]:
    """Prove that eight real client timelines sustained the measured window."""

    _validate_retrieval_case_rotation(samples)
    by_client: dict[str, list[tuple[float, float]]] = {}
    events: list[tuple[float, int]] = []
    for sample in samples:
        started = float(sample["started_monotonic_ms"])
        completed = float(sample["completed_monotonic_ms"])
        by_client.setdefault(str(sample["client_id"]), []).append(
            (started, completed)
        )
        events.extend(((started, 1), (completed, -1)))
    if set(by_client) != _EXPECTED_RETRIEVAL_CLIENT_IDS or concurrency != len(
        _EXPECTED_RETRIEVAL_CLIENT_IDS
    ):
        raise ValueError("retrieval samples must use the exact eight client identities")

    # Treat intervals as half-open: a completion and a later request starting at
    # the same instant do not manufacture simultaneous work.
    active = 0
    peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        if active < 0:
            raise ValueError("retrieval request concurrency timeline is invalid")
        peak = max(peak, active)
    if active != 0:
        raise ValueError("retrieval request concurrency timeline is incomplete")
    if peak < concurrency:
        raise ValueError("retrieval requests never reached eight concurrent calls")

    global_start = min(
        start for intervals in by_client.values() for start, _ in intervals
    )
    global_end = max(
        end for intervals in by_client.values() for _, end in intervals
    )
    minimum_client_span_ms = 300_000.0
    maximum_gap = 0.0
    for client_id, intervals in sorted(by_client.items()):
        ordered = sorted(intervals)
        first_start = ordered[0][0]
        last_completion = max(item[1] for item in ordered)
        if first_start - global_start > _MAX_RETRIEVAL_CLIENT_IDLE_MS:
            raise ValueError(f"retrieval client {client_id} joined the load window late")
        if global_end - last_completion > _MAX_RETRIEVAL_CLIENT_IDLE_MS:
            raise ValueError(f"retrieval client {client_id} left the load window early")
        if last_completion - first_start + 1e-6 < minimum_client_span_ms:
            raise ValueError(
                f"retrieval client {client_id} did not sustain five minutes"
            )
        previous_completion: float | None = None
        for started, completed in ordered:
            if previous_completion is not None:
                gap = started - previous_completion
                if gap < -1e-6:
                    raise ValueError(
                        f"retrieval client {client_id} has overlapping requests"
                    )
                maximum_gap = max(maximum_gap, gap)
                if gap > _MAX_RETRIEVAL_CLIENT_IDLE_MS:
                    raise ValueError(
                        f"retrieval client {client_id} was idle during sustained load"
                    )
            previous_completion = completed

    total_active_ms = math.fsum(
        completed - started
        for intervals in by_client.values()
        for started, completed in intervals
    )
    global_duration_ms = global_end - global_start
    if not _same_number(global_duration_ms / 1_000, declared_duration_seconds):
        raise ValueError("declared sustained duration does not match client timelines")
    return {
        "maximum_client_idle_ms": maximum_gap,
        "mean_active_requests": total_active_ms / global_duration_ms,
        "peak_active_requests": peak,
    }


def _validate_request_samples(
    value: Any,
    *,
    answer_samples: int,
    concurrency: int,
    declared_duration_seconds: float,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, int | float]],
    dict[str, int],
    float,
    float,
    float,
    dict[str, int | float],
    list[str],
    dict[str, Any],
]:
    sections = _exact_mapping(
        value, _REQUEST_SAMPLE_SECTIONS, "request samples"
    )
    normalized: dict[str, list[dict[str, Any]]] = {}
    percentiles: dict[str, dict[str, int | float]] = {}
    all_ids: set[str] = set()
    answer_case_ids: set[str] = set()
    answer_expectations: list[dict[str, Any]] = []
    retrieval_case_ids: set[str] = set()
    retrieval_expectations: dict[str, dict[str, Any]] = {}
    retrieval_clients: set[str] = set()
    retrieval_start: float | None = None
    retrieval_end: float | None = None
    status_counts = {"2xx": 0, "4xx": 0, "429": 0, "5xx": 0, "total": 0}
    semantic_failures: list[str] = []

    for section_id in sorted(_REQUEST_SAMPLE_SECTIONS):
        raw_samples = sections[section_id]
        if not isinstance(raw_samples, list) or not raw_samples:
            raise ValueError(f"{section_id} request samples must be a non-empty list")
        checked: list[dict[str, Any]] = []
        latencies: list[float] = []
        for index, raw_sample in enumerate(raw_samples):
            sample = _exact_mapping(
                raw_sample,
                _REQUEST_SAMPLE_FIELDS,
                f"{section_id} request sample {index}",
            )
            answer_evidence = sample["answer_evidence"]
            if section_id == "retrieval" and answer_evidence is not None:
                raise ValueError("retrieval requests must not contain answer commitments")
            if section_id == "answer" and not isinstance(answer_evidence, Mapping):
                raise ValueError(
                    "answer requests require prose-redacted checksum commitments"
                )
            request_id = _text(
                sample["request_id"],
                f"{section_id} request sample {index} request_id",
            )
            if request_id in all_ids:
                raise ValueError("request sample IDs must be globally unique")
            all_ids.add(request_id)
            client_id = _text(
                sample["client_id"],
                f"{section_id} request sample {index} client_id",
            )
            dataset_id = _text(
                sample["dataset_id"],
                f"{section_id} request sample {index} dataset_id",
            )
            case_id = _text(
                sample["case_id"],
                f"{section_id} request sample {index} case_id",
            )
            embedding_space_id = _text(
                sample["embedding_space_id"],
                f"{section_id} request sample {index} embedding_space_id",
            )
            query_vector_checksum = _digest(
                sample["query_vector_checksum"],
                f"{section_id} request sample {index} query_vector_checksum",
            )
            if section_id == "answer":
                if dataset_id != "gold-v1" or case_id not in _ANSWER_SAMPLE_CASE_IDS:
                    raise ValueError(
                        "answer request samples must use the fixed gold-v1 cases"
                    )
                if case_id in answer_case_ids:
                    raise ValueError("answer request case IDs must be unique")
                answer_case_ids.add(case_id)
            else:
                if dataset_id != "load-v1" or case_id not in _LOAD_SAMPLE_CASE_IDS:
                    raise ValueError(
                        "retrieval request samples must use load-v1 anchors"
                    )
                retrieval_case_ids.add(case_id)
            started = _finite(
                sample["started_monotonic_ms"],
                f"{section_id} request sample {index} started_monotonic_ms",
            )
            completed = _finite(
                sample["completed_monotonic_ms"],
                f"{section_id} request sample {index} completed_monotonic_ms",
            )
            if completed < started:
                raise ValueError("request completion cannot precede request start")
            retrieval_stage_ms = _finite(
                sample["retrieval_stage_ms"],
                f"{section_id} request sample {index} retrieval_stage_ms",
            )
            if retrieval_stage_ms <= 0:
                raise ValueError("request retrieval_stage_ms must be positive")
            if retrieval_stage_ms > completed - started + 1e-6:
                raise ValueError(
                    "request retrieval_stage_ms cannot exceed HTTP request duration"
                )
            status_code = sample["status_code"]
            if isinstance(status_code, bool) or not isinstance(status_code, int):
                raise ValueError("request status_code must be an integer")
            bucket = _status_bucket(status_code)
            selected_count = _count(
                sample["selected_chunk_count"],
                f"{section_id} request sample {index} selected_chunk_count",
            )
            unauthorized_count = _count(
                sample["unauthorized_chunk_count"],
                f"{section_id} request sample {index} unauthorized_chunk_count",
            )
            inactive_count = _count(
                sample["inactive_version_count"],
                f"{section_id} request sample {index} inactive_version_count",
            )
            semantic_success = sample["semantic_success"]
            if not isinstance(semantic_success, bool):
                raise ValueError("request semantic_success must be boolean")
            domain_status = _optional_text(
                sample["domain_status"],
                f"{section_id} request sample {index} domain_status",
            )
            domain_failure_code = _optional_text(
                sample["domain_failure_code"],
                f"{section_id} request sample {index} domain_failure_code",
            )
            error_code = _optional_text(
                sample["error_code"],
                f"{section_id} request sample {index} error_code",
            )
            expected_chunk_ids = _test_ids(
                sample["expected_chunk_ids"],
                f"{section_id} request sample {index} expected_chunk_ids",
            )
            if not expected_chunk_ids:
                raise ValueError("request expected Chunk IDs must not be empty")
            if section_id == "answer":
                answer_expectations.append(
                    {
                        "case_id": case_id,
                        "dataset_id": dataset_id,
                        "embedding_space_id": embedding_space_id,
                        "expected_chunk_ids": expected_chunk_ids,
                        "query_vector_checksum": query_vector_checksum,
                    }
                )
            else:
                expectation = {
                    "case_id": case_id,
                    "dataset_id": dataset_id,
                    "embedding_space_id": embedding_space_id,
                    "expected_chunk_ids": expected_chunk_ids,
                    "query_vector_checksum": query_vector_checksum,
                }
                previous = retrieval_expectations.setdefault(case_id, expectation)
                if previous != expectation:
                    raise ValueError(
                        "retrieval request expectations changed within a load case"
                    )
            selected_ids = _test_ids(
                sample["selected_chunk_ids"],
                f"{section_id} request sample {index} selected_chunk_ids",
            )
            visible_ids = _test_ids(
                sample["visible_chunk_ids"],
                f"{section_id} request sample {index} visible_chunk_ids",
            )
            unauthorized_ids = _test_ids(
                sample["unauthorized_chunk_ids"],
                f"{section_id} request sample {index} unauthorized_chunk_ids",
            )
            inactive_ids = _test_ids(
                sample["inactive_chunk_ids"],
                f"{section_id} request sample {index} inactive_chunk_ids",
            )
            if selected_count != len(selected_ids):
                raise ValueError("selected Chunk count does not match selected IDs")
            if unauthorized_count != len(unauthorized_ids):
                raise ValueError("unauthorized Chunk count does not match IDs")
            if inactive_count != len(inactive_ids):
                raise ValueError("inactive Chunk count does not match IDs")
            if not set(selected_ids) <= set(visible_ids):
                raise ValueError("selected Chunk IDs must be present in visible IDs")
            if not set(unauthorized_ids) <= set(visible_ids):
                raise ValueError("unauthorized Chunk IDs must be present in visible IDs")
            if not set(inactive_ids) <= set(visible_ids):
                raise ValueError("inactive Chunk IDs must be present in visible IDs")
            trace_id = _optional_text(
                sample["trace_id"],
                f"{section_id} request sample {index} trace_id",
            )
            independently_successful = (
                bucket == "2xx"
                and not set(expected_chunk_ids).isdisjoint(selected_ids)
                and unauthorized_count == 0
                and inactive_count == 0
                and error_code is None
                and trace_id is not None
                and (
                    section_id != "answer"
                    or (domain_status == "answered" and domain_failure_code is None)
                )
            )
            if semantic_success != independently_successful:
                raise ValueError(
                    f"{section_id} request sample {index} semantic result is inconsistent"
                )
            if not semantic_success:
                semantic_failures.append(request_id)
            latency = completed - started
            latencies.append(latency)
            checked.append(
                {
                    "answer_evidence": answer_evidence,
                    "case_id": case_id,
                    "client_id": client_id,
                    "completed_monotonic_ms": completed,
                    "dataset_id": dataset_id,
                    "domain_failure_code": domain_failure_code,
                    "domain_status": domain_status,
                    "embedding_space_id": embedding_space_id,
                    "error_code": error_code,
                    "expected_chunk_ids": expected_chunk_ids,
                    "inactive_chunk_ids": inactive_ids,
                    "inactive_version_count": inactive_count,
                    "request_id": request_id,
                    "query_vector_checksum": query_vector_checksum,
                    "retrieval_stage_ms": retrieval_stage_ms,
                    "selected_chunk_ids": selected_ids,
                    "selected_chunk_count": selected_count,
                    "semantic_success": semantic_success,
                    "started_monotonic_ms": started,
                    "status_code": status_code,
                    "trace_id": trace_id,
                    "unauthorized_chunk_ids": unauthorized_ids,
                    "unauthorized_chunk_count": unauthorized_count,
                    "visible_chunk_ids": visible_ids,
                }
            )
            if section_id == "retrieval":
                retrieval_clients.add(client_id)
                retrieval_start = (
                    started
                    if retrieval_start is None
                    else min(retrieval_start, started)
                )
                retrieval_end = (
                    completed
                    if retrieval_end is None
                    else max(retrieval_end, completed)
                )
                status_counts[bucket] += 1
                status_counts["total"] += 1
                if status_code == 429:
                    status_counts["429"] += 1
        if section_id == "answer" and len(checked) != answer_samples:
            raise ValueError("answer request samples do not match the workload")
        normalized[section_id] = sorted(checked, key=lambda item: item["request_id"])
        percentiles[section_id] = {
            "p50": nearest_rank_percentile(latencies, 0.50),
            "p95": nearest_rank_percentile(latencies, 0.95),
            "p99": nearest_rank_percentile(latencies, 0.99),
            "sample_count": len(latencies),
        }

    if answer_case_ids != _ANSWER_SAMPLE_CASE_IDS:
        raise ValueError("answer request case set does not match the fixed gold-v1 set")
    if _canonical_digest(sorted(answer_expectations, key=lambda item: item["case_id"])) != (
        _ANSWER_SAMPLE_EXPECTATIONS_SHA256
    ):
        raise ValueError("answer request expectations do not match gold-v1")
    if retrieval_case_ids != _LOAD_SAMPLE_CASE_IDS:
        raise ValueError("retrieval request evidence must cover all load-v1 anchors")
    if _canonical_digest(
        sorted(retrieval_expectations.values(), key=lambda item: item["case_id"])
    ) != _LOAD_SAMPLE_EXPECTATIONS_SHA256:
        raise ValueError("retrieval request expectations do not match load-v1")

    answer_commitments, http_answer_metrics = evaluate_http_answer_commitments(
        {
            str(item["case_id"]): item["answer_evidence"]
            for item in normalized["answer"]
        }
    )
    for item in normalized["answer"]:
        item["answer_evidence"] = answer_commitments[str(item["case_id"])]
    unit_rates = (
        "answer_correctness",
        "citation_coverage",
        "citation_precision",
        "numerical_fidelity",
        "supported_claim_rate",
    )
    if (
        any(float(http_answer_metrics[field]) != 1.0 for field in unit_rates)
        or int(http_answer_metrics["generation_failure_count"]) != 0
        or int(http_answer_metrics["forbidden_answer_exposure_count"]) != 0
        or any(
            http_answer_metrics[field] is not None
            and float(http_answer_metrics[field]) != 1.0
            for field in ("conflict_handling_rate", "temporal_comparison_rate")
        )
    ):
        raise ValueError("HTTP answer commitments do not pass fixed gold metrics")

    if len(retrieval_clients) != concurrency:
        raise ValueError("retrieval samples must contain exactly eight clients")
    assert retrieval_start is not None and retrieval_end is not None
    measured_duration = (retrieval_end - retrieval_start) / 1_000
    if measured_duration <= 0:
        raise ValueError("retrieval request timeline must have positive duration")
    if not _same_number(measured_duration, declared_duration_seconds):
        raise ValueError("declared sustained duration does not match request timeline")
    concurrency_diagnostics = _retrieval_concurrency_diagnostics(
        normalized["retrieval"],
        concurrency=concurrency,
        declared_duration_seconds=declared_duration_seconds,
    )
    successful = sum(
        item["semantic_success"] for item in normalized["retrieval"]
    )
    retrieval_rps = successful / measured_duration
    server_error_rate = status_counts["5xx"] / status_counts["total"]
    return (
        normalized,
        percentiles,
        status_counts,
        measured_duration,
        retrieval_rps,
        server_error_rate,
        concurrency_diagnostics,
        sorted(semantic_failures),
        http_answer_metrics,
    )


def _validate_latencies(value: Any) -> dict[str, dict[str, int | float]]:
    latency = _exact_mapping(value, _LATENCY_FIELDS, "latency observations")
    result: dict[str, dict[str, int | float]] = {}
    for name in sorted(_LATENCY_FIELDS):
        samples = latency[name]
        if not isinstance(samples, list) or not samples:
            raise ValueError(f"{name} latency samples must be a non-empty list")
        checked = [_finite(sample, f"{name} latency sample") for sample in samples]
        result[name] = {
            "p50": nearest_rank_percentile(checked, 0.50),
            "p95": nearest_rank_percentile(checked, 0.95),
            "p99": nearest_rank_percentile(checked, 0.99),
            "sample_count": len(checked),
        }
    return result


def _validate_cost(
    value: Any,
    *,
    answer_samples: int,
    retrieval_samples: int,
    provider_mode: str,
) -> dict[str, int | float | str]:
    cost = _exact_mapping(value, _COST_FIELDS, "production cost")
    if cost["currency"] != "USD":
        raise ValueError("production cost currency must be USD")
    raw_costs = cost["request_cost_usd"]
    if not isinstance(raw_costs, list) or not raw_costs:
        raise ValueError("request_cost_usd must be a non-empty list")
    costs = [
        _finite(item, f"request_cost_usd item {index}")
        for index, item in enumerate(raw_costs)
    ]
    total = math.fsum(sorted(costs))
    metered = len(costs)
    model_calls = _count(cost["model_calls"], "model_calls", positive=True)
    input_tokens = _count(cost["input_tokens"], "input_tokens")
    output_tokens = _count(cost["output_tokens"], "output_tokens")
    if input_tokens == 0 or output_tokens == 0:
        raise ValueError("production model usage must include input and output tokens")
    if metered != answer_samples:
        raise ValueError("cost samples must exactly cover measured answer requests")
    expected_calls = retrieval_samples + (2 * answer_samples)
    if model_calls != expected_calls:
        raise ValueError("model calls do not exactly cover measured requests")
    if provider_mode == "deterministic_reference":
        expected_input = (retrieval_samples + answer_samples) * 12
        expected_input += answer_samples * 96
        expected_output = answer_samples * 48
        if input_tokens != expected_input or output_tokens != expected_output:
            raise ValueError("deterministic provider token accounting is inconsistent")
    return {
        "currency": "USD",
        "estimated_total_usd": total,
        "input_tokens": input_tokens,
        "mean_request_usd": total / metered,
        "metered_requests": metered,
        "model_calls": model_calls,
        "output_tokens": output_tokens,
        "request_cost_sample_count": metered,
    }


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _expected_scenario_outcome(scenario_id: str) -> dict[str, Any]:
    if scenario_id == "llm_failure":
        return {
            "domain_status": "refused",
            "error_code": None,
            "http_status": 200,
            "reason": "invalid_model_output",
        }
    if scenario_id == "llm_success":
        return {
            "domain_status": "answered",
            "error_code": None,
            "http_status": 200,
            "reason": None,
        }
    if scenario_id == "neo4j_success":
        return {
            "domain_status": "retrieved",
            "error_code": None,
            "http_status": 200,
            "reason": None,
        }
    if scenario_id == "neo4j_failure":
        return {
            "domain_status": None,
            "error_code": "internal_error",
            "http_status": 500,
            "reason": None,
        }
    if scenario_id in _LIFECYCLE_SCENARIOS or scenario_id.endswith("_success"):
        return {
            "domain_status": None,
            "error_code": None,
            "http_status": "2xx",
            "reason": None,
        }
    if scenario_id.endswith("_timeout"):
        return {
            "domain_status": None,
            "error_code": "dependency_timeout",
            "http_status": 504,
            "reason": None,
        }
    return {
        "domain_status": None,
        "error_code": "dependency_unavailable",
        "http_status": 503,
        "reason": None,
    }


def _http_outcome_matches(observed: int, expected: int | str) -> bool:
    return 200 <= observed <= 299 if expected == "2xx" else observed == expected


def _access_response_ids(
    response: Mapping[str, Any],
) -> tuple[set[str], set[str], set[str]]:
    trace = response.get("trace", {})
    if not isinstance(trace, Mapping):
        raise ValueError("access-isolation response trace must be an object")
    selected = trace.get("selected_chunk_ids", [])
    if not isinstance(selected, list):
        raise ValueError("access-isolation trace selection must be an array")
    trace_ids = {str(item) for item in selected if isinstance(item, str)}
    for stage in _ACCESS_TRACE_STAGES:
        values = trace.get(stage, [])
        if not isinstance(values, list):
            raise ValueError("access-isolation trace stages must be arrays")
        trace_ids.update(
            str(item["chunk_id"])
            for item in values
            if isinstance(item, Mapping) and isinstance(item.get("chunk_id"), str)
        )
    chunks = response.get("chunks", [])
    citations = response.get("citations", [])
    if not isinstance(chunks, list) or not isinstance(citations, list):
        raise ValueError("access-isolation results and citations must be arrays")
    result_ids = {
        str(item["citation"]["chunk_id"])
        for item in chunks
        if isinstance(item, Mapping)
        and isinstance(item.get("citation"), Mapping)
        and isinstance(item["citation"].get("chunk_id"), str)
    }
    citation_ids = {
        str(item["chunk_id"])
        for item in citations
        if isinstance(item, Mapping) and isinstance(item.get("chunk_id"), str)
    }
    return trace_ids, result_ids, citation_ids


def _access_nested_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _access_nested_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _access_nested_strings(item)


def _validate_access_evidence(
    value: Any,
    *,
    event_started_ms: float,
    event_completed_ms: float,
) -> dict[str, Any]:
    evidence = _exact_mapping(
        value,
        _ACCESS_EVIDENCE_FIELDS,
        "access-isolation evidence",
    )
    if evidence["dataset_id"] != "load-v1":
        raise ValueError("access-isolation evidence must use load-v1")
    if evidence["schema_version"] != "load-v1-access-isolation-v1":
        raise ValueError("access-isolation evidence schema version is invalid")
    principal = _exact_mapping(
        evidence["principal"],
        frozenset({"groups", "tenant_id"}),
        "access-isolation principal",
    )
    if dict(principal) != _EXPECTED_ACCESS_PRINCIPAL:
        raise ValueError("access-isolation principal drifted from load-v1")
    inventory = _exact_mapping(
        evidence["inventory"],
        frozenset(_EXPECTED_ACCESS_INVENTORY),
        "access-isolation inventory",
    )
    normalized_inventory: dict[str, dict[str, Any]] = {}
    for inventory_id, expected in sorted(_EXPECTED_ACCESS_INVENTORY.items()):
        item = _exact_mapping(
            inventory[inventory_id],
            frozenset({"count", "chunk_ids_sha256"}),
            f"access-isolation inventory {inventory_id}",
        )
        normalized_item = {
            "count": _count(
                item["count"],
                f"access-isolation inventory {inventory_id} count",
                positive=True,
            ),
            "chunk_ids_sha256": _digest(
                item["chunk_ids_sha256"],
                f"access-isolation inventory {inventory_id} digest",
            ),
        }
        if normalized_item != expected:
            raise ValueError("access-isolation inventory does not match load-v1")
        normalized_inventory[inventory_id] = normalized_item

    raw_probes = evidence["probes"]
    if not isinstance(raw_probes, list) or len(raw_probes) != 8:
        raise ValueError("access-isolation evidence must contain eight HTTP probes")
    normalized_probes: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    request_ids: set[str] = set()
    trace_ids: set[str] = set()
    for index, raw_probe in enumerate(raw_probes):
        probe = _exact_mapping(
            raw_probe,
            _ACCESS_PROBE_FIELDS,
            f"access-isolation probe {index}",
        )
        case_id = _text(probe["case_id"], f"access-isolation probe {index} case_id")
        expectation = _EXPECTED_ACCESS_PROBES.get(case_id)
        if expectation is None or case_id in case_ids:
            raise ValueError("access-isolation canary identity is invalid")
        case_ids.add(case_id)
        request_id = _text(
            probe["request_id"], f"access-isolation probe {case_id} request_id"
        )
        trace_id = _text(
            probe["trace_id"], f"access-isolation probe {case_id} trace_id"
        )
        if request_id in request_ids or trace_id in trace_ids:
            raise ValueError("access-isolation request and trace IDs must be unique")
        request_ids.add(request_id)
        trace_ids.add(trace_id)
        started = _finite(
            probe["started_monotonic_ms"],
            f"access-isolation probe {case_id} start",
        )
        completed = _finite(
            probe["completed_monotonic_ms"],
            f"access-isolation probe {case_id} completion",
        )
        if (
            completed < started
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
            if probe[field] != expectation[field]:
                raise ValueError("access-isolation canary drifted from load-v1")
        query_text_sha256 = _digest(
            probe["query_text_sha256"],
            f"access-isolation probe {case_id} query text digest",
        )
        query_vector_checksum = _digest(
            probe["query_vector_checksum"],
            f"access-isolation probe {case_id} query vector digest",
        )
        if (
            query_text_sha256 != expectation["query_text_sha256"]
            or query_vector_checksum != expectation["query_vector_checksum"]
        ):
            raise ValueError("access-isolation query identity drifted from load-v1")
        response = probe["response"]
        if not isinstance(response, Mapping):
            raise ValueError("access-isolation response must be an object")
        observed_trace, observed_results, observed_citations = _access_response_ids(
            response
        )
        declared_trace = _test_ids(
            probe["trace_chunk_ids"],
            f"access-isolation probe {case_id} trace Chunk IDs",
        )
        declared_results = _test_ids(
            probe["result_chunk_ids"],
            f"access-isolation probe {case_id} result Chunk IDs",
        )
        declared_citations = _test_ids(
            probe["citation_chunk_ids"],
            f"access-isolation probe {case_id} citation Chunk IDs",
        )
        if (
            declared_trace != sorted(observed_trace)
            or declared_results != sorted(observed_results)
            or declared_citations != sorted(observed_citations)
        ):
            raise ValueError("access-isolation response IDs are not independently bound")
        strings = tuple(_access_nested_strings(response))
        exposes_marker = any(
            marker in item
            for marker in expectation["markers"]
            for item in strings
        ) or any(
            f"sha256:{hashlib.sha256(item.encode('utf-8')).hexdigest()}"
            == expectation["source_text_sha256"]
            for item in strings
        )
        trace = response.get("trace", {})
        nonempty_trace = any(
            trace.get(stage, []) for stage in _ACCESS_TRACE_STAGES
        ) or bool(trace.get("decisions", []))
        http_status = probe["http_status"]
        if (
            isinstance(http_status, bool)
            or http_status != 200
            or probe["error_code"] is not None
            or observed_trace
            or observed_results
            or observed_citations
            or response.get("chunks", [])
            or response.get("citations", [])
            or nonempty_trace
            or exposes_marker
        ):
            raise ValueError("access-isolation probe exposed a protected existence signal")
        normalized_probes.append(
            {
                "canary_chunk_id": expectation["canary_chunk_id"],
                "case_id": case_id,
                "citation_chunk_ids": declared_citations,
                "completed_monotonic_ms": completed,
                "embedding_space_id": expectation["embedding_space_id"],
                "error_code": None,
                "http_status": 200,
                "kind": expectation["kind"],
                "query_text_sha256": query_text_sha256,
                "query_vector_checksum": query_vector_checksum,
                "request_id": request_id,
                "response": dict(response),
                "result_chunk_ids": declared_results,
                "started_monotonic_ms": started,
                "target_tenant_id": expectation["target_tenant_id"],
                "trace_chunk_ids": declared_trace,
                "trace_id": trace_id,
                "version_id": expectation["version_id"],
            }
        )
    if case_ids != set(_EXPECTED_ACCESS_PROBES):
        raise ValueError("access-isolation canary coverage is incomplete")
    return {
        "dataset_id": "load-v1",
        "inventory": normalized_inventory,
        "principal": dict(_EXPECTED_ACCESS_PRINCIPAL),
        "probes": sorted(normalized_probes, key=lambda item: item["case_id"]),
        "schema_version": "load-v1-access-isolation-v1",
    }


def _validate_fault_timeline(value: Any) -> dict[str, dict[str, Any]]:
    timeline = _exact_mapping(value, _SCENARIO_IDS, "fault timeline")
    normalized: dict[str, dict[str, Any]] = {}
    for scenario_id in sorted(_SCENARIO_IDS):
        event = _exact_mapping(
            timeline[scenario_id],
            (
                _ACCESS_FAULT_EVENT_FIELDS
                if scenario_id == "access_isolation"
                else _FAULT_EVENT_FIELDS
            ),
            f"fault event {scenario_id}",
        )
        started = _finite(
            event["started_monotonic_ms"],
            f"fault event {scenario_id} started_monotonic_ms",
        )
        completed = _finite(
            event["completed_monotonic_ms"],
            f"fault event {scenario_id} completed_monotonic_ms",
        )
        if completed < started:
            raise ValueError(f"fault event {scenario_id} completion precedes start")
        error_code = event["error_code"]
        if error_code is not None and (
            not isinstance(error_code, str) or error_code not in _ERROR_CODES
        ):
            raise ValueError(f"fault event {scenario_id} error_code is invalid")
        http_status = event["http_status"]
        if (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 200 <= http_status <= 599
        ):
            raise ValueError(f"fault event {scenario_id} http_status is invalid")
        normalized_event = {
            "assertion_failures": _notes(
                event["assertion_failures"],
                f"fault event {scenario_id} assertion_failures",
            ),
            "completed_monotonic_ms": completed,
            "domain_status": _optional_text(
                event["domain_status"],
                f"fault event {scenario_id} domain_status",
            ),
            "error_code": error_code,
            "http_status": http_status,
            "reason": _optional_text(
                event["reason"], f"fault event {scenario_id} reason"
            ),
            "started_monotonic_ms": started,
        }
        if scenario_id == "access_isolation":
            normalized_event["access_evidence"] = _validate_access_evidence(
                event["access_evidence"],
                event_started_ms=started,
                event_completed_ms=completed,
            )
        normalized[scenario_id] = normalized_event
    return normalized


def _validate_scenarios(
    value: Any,
    fault_timeline: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    scenarios = _exact_mapping(value, _SCENARIO_IDS, "production scenarios")
    normalized: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for scenario_id in sorted(_SCENARIO_IDS):
        scenario = _exact_mapping(
            scenarios[scenario_id],
            _SCENARIO_FIELDS,
            f"scenario {scenario_id}",
        )
        passed = scenario["passed"]
        if not isinstance(passed, bool):
            raise ValueError(f"scenario {scenario_id} passed must be boolean")
        latency = _finite(scenario["latency_ms"], f"scenario {scenario_id} latency_ms")
        error_code = scenario["error_code"]
        if error_code is not None and (
            not isinstance(error_code, str) or error_code not in _ERROR_CODES
        ):
            raise ValueError(f"scenario {scenario_id} error_code is invalid")
        http_status = scenario["http_status"]
        if (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 200 <= http_status <= 599
        ):
            raise ValueError(f"scenario {scenario_id} http_status is invalid")
        domain_status = _optional_text(
            scenario["domain_status"], f"scenario {scenario_id} domain_status"
        )
        reason = _optional_text(scenario["reason"], f"scenario {scenario_id} reason")
        expected = _expected_scenario_outcome(scenario_id)
        fault_event = fault_timeline[scenario_id]
        timeline_latency = (
            float(fault_event["completed_monotonic_ms"])
            - float(fault_event["started_monotonic_ms"])
        )
        if not _same_number(latency, timeline_latency):
            raise ValueError(
                f"scenario {scenario_id} latency does not match fault timeline"
            )
        if scenario_id in _PROVIDER_TIMEOUT_SCENARIO_IDS and not (
            _PROVIDER_TIMEOUT_MIN_MS <= latency <= _PROVIDER_TIMEOUT_MAX_MS
        ):
            raise ValueError(
                f"scenario {scenario_id} did not return at the five-second API deadline"
            )
        if error_code != fault_event["error_code"]:
            raise ValueError(
                f"scenario {scenario_id} error code does not match fault timeline"
            )
        if http_status != fault_event["http_status"]:
            raise ValueError(
                f"scenario {scenario_id} HTTP status does not match fault timeline"
            )
        if domain_status != fault_event["domain_status"]:
            raise ValueError(
                f"scenario {scenario_id} domain status does not match fault timeline"
            )
        if reason != fault_event["reason"]:
            raise ValueError(
                f"scenario {scenario_id} reason does not match fault timeline"
            )
        timeline_passed = not fault_event["assertion_failures"]
        if passed != timeline_passed:
            raise ValueError(
                f"scenario {scenario_id} pass flag does not match fault assertions"
            )
        normalized[scenario_id] = {
            "assertion_failures": fault_event["assertion_failures"],
            "domain_status": domain_status,
            "error_code": error_code,
            "expected": expected,
            "http_status": http_status,
            "latency_ms": latency,
            "passed": passed,
            "reason": reason,
        }
        if not passed:
            failures.append(f"scenario {scenario_id} did not pass")
        if error_code != expected["error_code"]:
            failures.append(
                f"scenario {scenario_id} returned error code {error_code!r}; "
                f"expected {expected['error_code']!r}"
            )
        if not _http_outcome_matches(http_status, expected["http_status"]):
            failures.append(
                f"scenario {scenario_id} returned HTTP {http_status}; "
                f"expected {expected['http_status']}"
            )
        if domain_status != expected["domain_status"]:
            failures.append(
                f"scenario {scenario_id} returned domain status {domain_status!r}; "
                f"expected {expected['domain_status']!r}"
            )
        if reason != expected["reason"]:
            failures.append(
                f"scenario {scenario_id} returned reason {reason!r}; "
                f"expected {expected['reason']!r}"
            )
    return normalized, failures


def _test_ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    normalized = [_text(item, f"{name} item") for item in value]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} entries must be unique")
    return sorted(normalized)


def _validate_suite_results(
    value: Any,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    suites = _exact_mapping(value, _SUITE_IDS, "production suite results")
    normalized: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for suite_id in sorted(_SUITE_IDS):
        suite = _exact_mapping(
            suites[suite_id],
            _SUITE_RESULT_FIELDS,
            f"suite result {suite_id}",
        )
        tests_run = _count(
            suite["tests_run"], f"suite result {suite_id} tests_run", positive=True
        )
        categories = {
            field: _test_ids(
                suite[field], f"suite result {suite_id} {field}"
            )
            for field in (
                "error_test_ids",
                "failed_test_ids",
                "passed_test_ids",
                "skipped_test_ids",
            )
        }
        all_ids = [test_id for items in categories.values() for test_id in items]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError(f"suite result {suite_id} test IDs overlap")
        if len(all_ids) != tests_run:
            raise ValueError(f"suite result {suite_id} coverage is incomplete")
        normalized[suite_id] = {"tests_run": tests_run, **categories}
        for category in ("error_test_ids", "failed_test_ids", "skipped_test_ids"):
            if categories[category]:
                failures.append(
                    f"suite {suite_id} contains {len(categories[category])} "
                    f"{category.removesuffix('_test_ids').replace('_', ' ')} test(s)"
                )
    return normalized, failures


def _validate_canonical_graph_state(value: Any, name: str) -> dict[str, Any]:
    state = _exact_mapping(value, _CANONICAL_GRAPH_STATE_FIELDS, name)
    if state["schema_and_indexes_verified"] is not True:
        raise ValueError(f"{name} does not verify schema and indexes")
    node_count = _count(
        state["business_node_count"], f"{name} node count", positive=True
    )
    relationship_count = _count(
        state["business_relationship_count"],
        f"{name} relationship count",
        positive=True,
    )
    labels_raw = state["label_counts"]
    if not isinstance(labels_raw, Mapping) or not labels_raw:
        raise ValueError(f"{name} label_counts must be a non-empty object")
    labels = {
        _text(label, f"{name} label"): _count(
            count, f"{name} {label} count", positive=True
        )
        for label, count in labels_raw.items()
    }
    if any(count > node_count for count in labels.values()):
        raise ValueError(f"{name} label count exceeds the business node count")
    return {
        "business_node_count": node_count,
        "business_relationship_count": relationship_count,
        "label_counts": dict(sorted(labels.items())),
        "schema_and_indexes_verified": True,
        "sha256": _digest(state["sha256"], f"{name} digest"),
    }


def _validate_canonical_graph(
    value: Any,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    graph = _exact_mapping(value, _CANONICAL_GRAPH_FIELDS, "canonical graph states")
    normalized = {
        field: _validate_canonical_graph_state(
            graph[field], f"canonical graph {field}"
        )
        for field in sorted(_CANONICAL_GRAPH_FIELDS)
    }
    failures: list[str] = []
    if normalized["pre_validation_state"] != normalized["post_validation_state"]:
        failures.append(
            "canonical graph changed across the read-only validation window"
        )
    if normalized["backup_source_state"] != normalized["restored_state"]:
        failures.append(
            "restored canonical graph does not match the backup source state"
        )
    return normalized, failures


def _validate_runtime_environment(
    value: Any,
    versions: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    environment = _exact_mapping(
        value, _RUNTIME_ENVIRONMENT_FIELDS, "runtime environment"
    )
    image = _text(environment["actual_neo4j_image"], "actual_neo4j_image")
    repo_digest = _digest(
        environment["actual_neo4j_repo_digest"], "actual_neo4j_repo_digest"
    )
    commit = _text(environment["code_commit"], "runtime code_commit")
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("runtime code_commit must be a full Git object ID")
    commit = commit.lower()
    if image != versions["neo4j_image"]:
        raise ValueError("actual Neo4j image does not match the version inventory")
    if repo_digest != versions["neo4j_image_digest"]:
        raise ValueError("actual Neo4j RepoDigest does not match the version inventory")
    if commit != versions["code_commit"]:
        raise ValueError("runtime code commit does not match the version inventory")
    nodes = _count(
        environment["database_initial_node_count"],
        "database_initial_node_count",
    )
    relationships = _count(
        environment["database_initial_relationship_count"],
        "database_initial_relationship_count",
    )
    image_id = _digest(environment["actual_neo4j_image_id"], "actual Neo4j image ID")
    memory = _count(environment["actual_memory_bytes"], "actual memory bytes", positive=True)
    memory_swap = _count(
        environment["actual_memory_swap_bytes"],
        "actual memory swap bytes",
        positive=True,
    )
    nano_cpus = _count(environment["actual_nano_cpus"], "actual nano CPUs", positive=True)
    if memory != 3_072 * 1_024 * 1_024 or memory_swap != memory or nano_cpus != 8_000_000_000:
        raise ValueError("actual Neo4j container resource envelope drifted")
    expected_settings = {
        "configured_heap_initial": "512m",
        "configured_heap_max": "1024m",
        "configured_pagecache": "512m",
        "configured_transaction_timeout": "300s",
    }
    for field, expected in expected_settings.items():
        if environment[field] != expected:
            raise ValueError(f"runtime {field} drifted")
    retrieval_timeout = _finite(
        environment["retrieval_transaction_timeout_seconds"],
        "retrieval transaction timeout",
    )
    readiness_timeout = _finite(
        environment["readiness_transaction_timeout_seconds"],
        "readiness transaction timeout",
    )
    if retrieval_timeout != 5 or readiness_timeout != 5:
        raise ValueError("online Neo4j transaction timeout envelope drifted")
    readiness_probe_status = _text(
        environment["readiness_probe_status"], "readiness_probe_status"
    )
    if readiness_probe_status != "ready":
        raise ValueError("runtime readiness probe did not pass")
    api_limit = _text(
        environment["api_process_resource_limit"],
        "api_process_resource_limit",
    )
    if api_limit != "host-default-unbounded":
        raise ValueError("API process resource disclosure is invalid")
    host_cpu = _count(environment["host_cpu_count"], "host CPU count", positive=True)
    if host_cpu < 8:
        raise ValueError("host CPU count cannot supply the Neo4j resource envelope")
    host_memory = _count(
        environment["host_memory_bytes"], "host memory bytes", positive=True
    )
    host_platform = _text(environment["host_platform"], "host platform")
    failures: list[str] = []
    if nodes or relationships:
        failures.append(
            "validation database was not clean before the production-reference run"
        )
    return (
        {
            "actual_memory_bytes": memory,
            "actual_memory_swap_bytes": memory_swap,
            "actual_nano_cpus": nano_cpus,
            "actual_neo4j_image": image,
            "actual_neo4j_image_id": image_id,
            "actual_neo4j_repo_digest": repo_digest,
            "api_process_resource_limit": api_limit,
            "code_commit": commit,
            **expected_settings,
            "database_initial_node_count": nodes,
            "database_initial_relationship_count": relationships,
            "host_cpu_count": host_cpu,
            "host_memory_bytes": host_memory,
            "host_platform": host_platform,
            "readiness_probe_status": readiness_probe_status,
            "readiness_transaction_timeout_seconds": readiness_timeout,
            "retrieval_transaction_timeout_seconds": retrieval_timeout,
        },
        failures,
    )


def _validate_provider_evidence(value: Any) -> dict[str, Any]:
    provider = _exact_mapping(
        value, _PROVIDER_EVIDENCE_FIELDS, "provider evidence"
    )
    mode = provider["mode"]
    if not isinstance(mode, str) or mode not in _PROVIDER_EVIDENCE_MODES:
        raise ValueError("provider evidence mode is invalid")
    answer_warmups = _count(
        provider["answer_warmup_model_calls"],
        "answer_warmup_model_calls",
    )
    if answer_warmups != _EXPECTED_ANSWER_WARMUP_MODEL_CALLS:
        raise ValueError("provider evidence does not prove the full answer preflight")
    answer_preflight_case_ids = _test_ids(
        provider["answer_preflight_case_ids"],
        "answer preflight case IDs",
    )
    if frozenset(answer_preflight_case_ids) != _ANSWER_SAMPLE_CASE_IDS:
        raise ValueError("provider evidence does not cover the fixed answer case set")
    return {
        "answer_preflight_case_ids": answer_preflight_case_ids,
        "answer_warmup_model_calls": answer_warmups,
        "measured_answer_model_calls": _count(
            provider["measured_answer_model_calls"],
            "measured_answer_model_calls",
            positive=True,
        ),
        "measured_embedding_model_calls": _count(
            provider["measured_embedding_model_calls"],
            "measured_embedding_model_calls",
            positive=True,
        ),
        "mode": mode,
        "peak_concurrency": _count(
            provider["peak_concurrency"],
            "provider peak_concurrency",
            positive=True,
        ),
    }


def _validate_quality_metric_mapping(
    value: Any,
    fields: frozenset[str],
    name: str,
) -> dict[str, int | float | None]:
    source = _exact_mapping(value, fields, name)
    count_fields = {
        "answerable_count",
        "citation_attachment_count",
        "exact_token_count",
        "expected_conflict_count",
        "expected_refusal_count",
        "expected_temporal_comparison_count",
        "forbidden_answer_exposure_count",
        "generation_failure_count",
        "item_count",
        "material_claim_count",
        "unauthorized_exposure_count",
    }
    nullable_rate_fields = {"conflict_handling_rate", "temporal_comparison_rate"}
    result: dict[str, int | float | None] = {}
    for field in sorted(fields):
        if field in count_fields:
            result[field] = _count(source[field], f"{name} {field}")
        elif field in nullable_rate_fields and source[field] is None:
            result[field] = None
        else:
            score = _finite(source[field], f"{name} {field}")
            if score > 1:
                raise ValueError(f"{name} {field} must be between zero and one")
            result[field] = score
    return result


def _validate_quality_evidence(
    value: Any,
) -> tuple[dict[str, Any], list[str]]:
    evidence = _exact_mapping(
        value,
        _QUALITY_EVIDENCE_FIELDS,
        "large-database quality evidence",
    )
    if evidence["schema_version"] != "production-large-database-quality-v1":
        raise ValueError("large-database quality evidence schema is invalid")
    if evidence["corpus_version"] != "1.0.1":
        raise ValueError("large-database quality corpus version is stale")
    try:
        answer_retrieval_limits = resolve_production_answer_retrieval_limits(
            {
                "answer": {
                    "retrieval_limits": evidence["answer_retrieval_limits"],
                },
                "profile_id": _PROFILE_ID,
                "schema_version": PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION,
                "version": PRODUCTION_REFERENCE_CONFIG_VERSION,
            }
        )
    except ValueError as error:
        raise ValueError(
            "large-database quality answer profile is not reviewed"
        ) from error
    if answer_retrieval_limits != PRODUCTION_ANSWER_RETRIEVAL_LIMITS:
        raise ValueError("large-database quality answer profile is not reviewed")
    configuration_digest = _digest(
        evidence["production_configuration_sha256"],
        "large-database quality production_configuration_sha256",
    )
    if configuration_digest != _REVIEWED_CONFIGURATION_FILE_SHA256:
        raise ValueError(
            "large-database quality configuration does not match the reviewed file"
        )
    prediction_identity = {
        "prediction_provider": _text(
            evidence["prediction_provider"],
            "large-database quality prediction_provider",
        ),
        "prediction_sha256": _digest(
            evidence["prediction_sha256"],
            "large-database quality prediction_sha256",
        ),
        "prediction_version": _text(
            evidence["prediction_version"],
            "large-database quality prediction_version",
        ),
    }
    expected_prediction_identity = {
        "prediction_provider": REFERENCE_PREDICTION_PROVIDER,
        "prediction_sha256": f"sha256:{REFERENCE_PREDICTION_SHA256}",
        "prediction_version": REFERENCE_PREDICTION_VERSION,
    }
    if prediction_identity != expected_prediction_identity:
        raise ValueError(
            "large-database quality prediction identity does not match the reviewed artifact"
        )
    case_count = _count(evidence["case_count"], "quality case_count", positive=True)
    case_ids = _test_ids(evidence["case_ids"], "quality case_ids")
    if case_count != 49 or len(case_ids) != case_count:
        raise ValueError("large-database quality evidence must cover exactly 49 cases")
    case_set_digest = _digest(
        evidence["case_set_sha256"],
        "large-database quality case_set_sha256",
    )
    if case_set_digest != f"sha256:{_GOLD_CASE_SET_SHA256}":
        raise ValueError("large-database quality case set does not match gold-v1")
    calculated_case_set = hashlib.sha256(
        (
            json.dumps(
                case_ids,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    if calculated_case_set != _GOLD_CASE_SET_SHA256:
        raise ValueError("large-database quality case IDs do not match gold-v1")

    case_evidence, recomputed_retrieval, recomputed_answers = (
        evaluate_quality_case_evidence(evidence["case_evidence"])
    )
    if (
        case_evidence["case_ids"] != case_ids
        or case_evidence["case_count"] != case_count
    ):
        raise ValueError("large-database raw case coverage does not match its summary")

    active = evidence["active_generation_ids"]
    if not isinstance(active, Mapping) or not active:
        raise ValueError("large-database quality generations must be a non-empty object")
    active_generations = {
        _text(tenant_id, "quality generation tenant"): _text(
            generation_id,
            f"quality generation {tenant_id}",
        )
        for tenant_id, generation_id in active.items()
    }
    if len(active_generations) != len(active):
        raise ValueError("large-database quality generation tenants must be unique")
    expected_generations = {
        tenant_id: embedding_index_generation_id(
            tenant_id,
            "19ef2d72-d978-5d0d-9f75-b7f33f9b6f4d",
            1,
        )
        for tenant_id in ("tenant-alpha", "tenant-beta")
    }
    if active_generations != expected_generations:
        raise ValueError(
            "large-database quality generations do not bind the reviewed corpus"
        )

    retrieval = _validate_quality_metric_mapping(
        evidence["retrieval_metrics"],
        _RETRIEVAL_QUALITY_FIELDS,
        "large-database retrieval metrics",
    )
    answers = _validate_quality_metric_mapping(
        evidence["answer_metrics"],
        _ANSWER_QUALITY_FIELDS,
        "large-database answer metrics",
    )
    if retrieval["item_count"] != case_count or answers["item_count"] != case_count:
        raise ValueError("large-database metric item counts do not match case evidence")
    for metric_id, recomputed in recomputed_retrieval.items():
        declared = retrieval[metric_id]
        if isinstance(recomputed, int):
            matches = declared == recomputed
        else:
            matches = _same_number(declared, recomputed)
        if not matches:
            raise ValueError(
                f"large-database retrieval metric {metric_id} does not match raw cases"
            )
    for metric_id, recomputed in recomputed_answers.items():
        declared = answers[metric_id]
        if declared is None or recomputed is None:
            matches = declared is recomputed
        elif isinstance(recomputed, int):
            matches = declared == recomputed
        else:
            matches = _same_number(declared, recomputed)
        if not matches:
            raise ValueError(
                f"large-database answer metric {metric_id} does not match raw cases"
            )
    failures = _notes(evidence["failures"], "large-database quality failures")
    passed = evidence["passed"]
    if not isinstance(passed, bool):
        raise ValueError("large-database quality passed must be boolean")
    if passed != (not failures):
        raise ValueError("large-database quality passed does not match failures")
    independently_failed = (
        retrieval["recall_at_5"] < 0.90
        or retrieval["mrr"] < 0.80
        or retrieval["ndcg_at_5"] < 0.85
        or retrieval["unauthorized_exposure_count"] != 0
        or answers["supported_claim_rate"] < 0.95
        or answers["citation_precision"] < 0.95
        or answers["citation_coverage"] < 0.95
        or answers["numerical_fidelity"] != 1.0
        or answers["refusal_f1"] < 0.90
        or answers["answer_correctness"] != 1.0
        or (
            answers["temporal_comparison_rate"] is not None
            and answers["temporal_comparison_rate"] != 1.0
        )
        or (
            answers["conflict_handling_rate"] is not None
            and answers["conflict_handling_rate"] != 1.0
        )
        or answers["generation_failure_count"] != 0
        or answers["forbidden_answer_exposure_count"] != 0
    )
    if passed == independently_failed:
        raise ValueError("large-database quality passed does not match measured metrics")
    projection_digest = _digest(
        evidence["gold_projection_sha256"],
        "large-database quality gold_projection_sha256",
    )
    if projection_digest != canonical_quality_digest(case_evidence):
        raise ValueError("large-database quality digest does not bind raw cases")
    graph_state_digest = _digest(
        evidence["graph_state_sha256"],
        "large-database quality graph_state_sha256",
    )
    normalized = {
        "active_generation_ids": dict(sorted(active_generations.items())),
        "answer_retrieval_limits": asdict(answer_retrieval_limits),
        "answer_metrics": answers,
        "case_count": case_count,
        "case_evidence": case_evidence,
        "case_ids": case_ids,
        "case_set_sha256": case_set_digest,
        "corpus_version": "1.0.1",
        "failures": failures,
        "gold_projection_sha256": projection_digest,
        "graph_state_sha256": graph_state_digest,
        "passed": passed,
        **prediction_identity,
        "production_configuration_sha256": configuration_digest,
        "retrieval_metrics": retrieval,
        "schema_version": "production-large-database-quality-v1",
    }
    return normalized, ([] if passed else ["large-database quality gates failed"])


def _count_mapping(
    value: Any,
    name: str,
    expected_keys: frozenset[str],
) -> dict[str, int]:
    source = _exact_mapping(value, expected_keys, name)
    return {
        key: _count(source[key], f"{name} {key}") for key in sorted(expected_keys)
    }


def _validate_ingestion_evidence(value: Any) -> dict[str, Any]:
    evidence = _exact_mapping(
        value,
        _INGESTION_EVIDENCE_FIELDS,
        "ingestion evidence",
    )
    if evidence["schema_version"] != "production-ingestion-observation-v2":
        raise ValueError("ingestion evidence schema is invalid")
    if evidence["clean_start"] is not True:
        raise ValueError("production ingestion did not start clean")
    started = _finite(evidence["started_monotonic_ms"], "ingestion started")
    completed = _finite(evidence["completed_monotonic_ms"], "ingestion completed")
    if completed <= started:
        raise ValueError("ingestion evidence duration must be positive")
    replay_started = _finite(
        evidence["replay_started_monotonic_ms"],
        "idempotent replay started",
    )
    replay_completed = _finite(
        evidence["replay_completed_monotonic_ms"],
        "idempotent replay completed",
    )
    query_ready = _finite(
        evidence["query_ready_monotonic_ms"],
        "query-ready observation",
    )
    if not completed < replay_started < replay_completed < query_ready:
        raise ValueError(
            "ingestion, replay, and query-ready lifecycle timeline is invalid"
        )
    total_versions = _count(evidence["total_versions"], "total_versions", positive=True)
    completed_versions = _count(
        evidence["completed_versions"], "completed_versions"
    )
    failed_versions = _count(evidence["failed_versions"], "failed_versions")
    if completed_versions + failed_versions != total_versions:
        raise ValueError("ingestion version outcome coverage is incomplete")
    database_documents = _count(
        evidence["database_documents"], "database_documents", positive=True
    )
    database_versions = _count(
        evidence["database_versions"], "database_versions", positive=True
    )
    idempotency_before = _digest(
        evidence["idempotency_before_state_sha256"],
        "idempotency before-state digest",
    )
    idempotency_after = _digest(
        evidence["idempotency_after_state_sha256"],
        "idempotency after-state digest",
    )
    idempotency_mismatches = _count(
        evidence["idempotency_mismatch_count"],
        "ingestion idempotency_mismatch_count",
    )
    initial_load_transaction_timeout = _finite(
        evidence["initial_load_transaction_timeout_seconds"],
        "initial-load transaction timeout",
    )
    if initial_load_transaction_timeout != 60:
        raise ValueError("initial-load transaction timeout drifted")
    interrupted_before = _digest(
        evidence["interrupted_before_state_sha256"],
        "interrupted ingestion before-state digest",
    )
    interrupted_after = _digest(
        evidence["interrupted_after_state_sha256"],
        "interrupted ingestion after-state digest",
    )
    if interrupted_before != interrupted_after:
        raise ValueError("interrupted ingestion changed canonical graph state")

    generations_raw = _exact_mapping(
        evidence["active_generations"],
        frozenset(_LOAD_TENANTS),
        "active load embedding generations",
    )
    active_generations = {
        tenant_id: _text(
            generations_raw[tenant_id],
            f"active load embedding generation {tenant_id}",
        )
        for tenant_id in _LOAD_TENANTS
    }
    if active_generations != _EXPECTED_LOAD_ACTIVE_GENERATIONS:
        raise ValueError("active embedding generations do not match load-v1")
    for tenant_id, generation_id in active_generations.items():
        if generation_id != embedding_index_generation_id(
            tenant_id,
            "ef155576-e476-579d-9ce8-b6e0a233d0a9",
            1,
        ):
            raise ValueError("active embedding generation identity is not derivable")

    coverage_raw = _exact_mapping(
        evidence["embedding_generation_coverage"],
        frozenset(_LOAD_TENANTS),
        "load embedding generation coverage",
    )
    embedding_coverage: dict[str, dict[str, Any]] = {}
    for tenant_id in _LOAD_TENANTS:
        tenant_coverage = _exact_mapping(
            coverage_raw[tenant_id],
            _EMBEDDING_COVERAGE_FIELDS,
            f"load embedding coverage {tenant_id}",
        )
        embedding_coverage[tenant_id] = {
            "covered_chunks": _count(
                tenant_coverage["covered_chunks"],
                f"load embedding covered chunks {tenant_id}",
                positive=True,
            ),
            "generation_id": _text(
                tenant_coverage["generation_id"],
                f"load embedding coverage generation {tenant_id}",
            ),
            "total_chunks": _count(
                tenant_coverage["total_chunks"],
                f"load embedding total chunks {tenant_id}",
                positive=True,
            ),
        }
    if embedding_coverage != _EXPECTED_LOAD_EMBEDDING_COVERAGE:
        raise ValueError("embedding coverage does not exactly match load-v1")

    acl_raw = _exact_mapping(
        evidence["acl_coverage"],
        _LOAD_ACL_COVERAGE_FIELDS,
        "load principal ACL coverage",
    )
    acl_coverage = {
        field: _count(acl_raw[field], f"load principal ACL {field}")
        for field in sorted(
            _LOAD_ACL_COVERAGE_FIELDS - {"access_groups", "tenant_id"}
        )
    }
    acl_coverage.update(
        {
            "access_groups": _test_ids(
                acl_raw["access_groups"], "load principal access groups"
            ),
            "tenant_id": _text(
                acl_raw["tenant_id"], "load principal tenant ID"
            ),
        }
    )
    acl_coverage = dict(sorted(acl_coverage.items()))
    if acl_coverage != _EXPECTED_LOAD_ACL_COVERAGE:
        raise ValueError("ACL coverage does not exactly match the load-v1 manifest")

    recovery_job_raw = _exact_mapping(
        evidence["recovered_job"],
        _RECOVERED_JOB_FIELDS,
        "recovered initial-load job",
    )
    recovered_job: dict[str, Any] = {}
    for field, expected in _EXPECTED_RECOVERED_INITIAL_LOAD_JOB.items():
        raw = recovery_job_raw[field]
        if isinstance(expected, int):
            recovered_job[field] = _count(
                raw, f"recovered initial-load job {field}"
            )
        elif field in {"request_fingerprint", "snapshot_manifest_hash"}:
            recovered_job[field] = _digest(
                raw, f"recovered initial-load job {field}"
            )
        elif expected == "":
            if raw != "":
                raise ValueError(
                    f"recovered initial-load job {field} must be empty"
                )
            recovered_job[field] = ""
        else:
            recovered_job[field] = _text(
                raw, f"recovered initial-load job {field}"
            )
    if recovered_job != _EXPECTED_RECOVERED_INITIAL_LOAD_JOB:
        raise ValueError("recovered job does not match the fixed load-v1 operation")
    recovered_job_id = _text(evidence["recovered_job_id"], "recovered_job_id")
    if recovered_job_id != recovered_job["job_id"]:
        raise ValueError("recovered job identity is not internally bound")
    interrupted_job_count = _count(
        evidence["interrupted_job_count"], "interrupted job count"
    )
    interrupted_task_node_count = _count(
        evidence["interrupted_task_node_count"],
        "interrupted task node count",
    )
    recovered_job_task_node_count = _count(
        evidence["recovered_job_task_node_count"],
        "recovered job task node count",
    )
    recovered_job_linked_task_count = _count(
        evidence["recovered_job_linked_task_count"],
        "recovered job linked task count",
    )
    if any(
        (
            interrupted_job_count,
            interrupted_task_node_count,
            recovered_job_task_node_count,
            recovered_job_linked_task_count,
        )
    ):
        raise ValueError(
            "atomic bulk recovery must not claim nonexistent durable task nodes"
        )
    recovery_checkpoint = _text(
        evidence["recovery_checkpoint"], "recovery checkpoint"
    )
    task_tracking_mode = _text(
        evidence["recovery_task_tracking_mode"], "recovery task tracking mode"
    )
    if (
        recovery_checkpoint != "BEFORE_PUBLISH"
        or task_tracking_mode != "aggregate_job_counters"
    ):
        raise ValueError("bulk recovery lifecycle semantics are invalid")

    fixed_counts = {
        "completed_versions": completed_versions,
        "database_documents": database_documents,
        "database_versions": database_versions,
        "failed_versions": failed_versions,
        "primary_tenant_active_chunks": _count(
            evidence["primary_tenant_active_chunks"],
            "primary_tenant_active_chunks",
            positive=True,
        ),
        "primary_visible_chunks": _count(
            evidence["primary_visible_chunks"],
            "primary_visible_chunks",
            positive=True,
        ),
        "replayed_active_versions": _count(
            evidence["replayed_active_versions"],
            "replayed_active_versions",
            positive=True,
        ),
        "submitted_chunks": _count(
            evidence["submitted_chunks"], "submitted_chunks", positive=True
        ),
        "total_active_chunks": _count(
            evidence["total_active_chunks"], "total_active_chunks", positive=True
        ),
        "total_historical_chunks": _count(
            evidence["total_historical_chunks"],
            "total_historical_chunks",
            positive=True,
        ),
        "total_versions": total_versions,
    }
    expected_counts = {
        "completed_versions": 480,
        "database_documents": 240,
        "database_versions": 480,
        "failed_versions": 0,
        "primary_tenant_active_chunks": 10_000,
        "primary_visible_chunks": 7_500,
        "replayed_active_versions": 240,
        "submitted_chunks": 24_000,
        "total_active_chunks": 12_000,
        "total_historical_chunks": 12_000,
        "total_versions": 480,
    }
    if fixed_counts != expected_counts:
        raise ValueError("ingestion counts do not exactly match committed load-v1")
    if (
        fixed_counts["primary_visible_chunks"]
        != acl_coverage["visible_same_tenant_active_chunks"]
        or sum(
            item["covered_chunks"] for item in embedding_coverage.values()
        )
        != fixed_counts["total_active_chunks"]
    ):
        raise ValueError("ingestion coverage is not internally complete")
    if idempotency_mismatches != 0 or idempotency_before != idempotency_after:
        raise ValueError("idempotent replay changed canonical graph state")
    return {
        "acl_coverage": acl_coverage,
        "active_generations": active_generations,
        "clean_start": True,
        "completed_monotonic_ms": completed,
        "completed_versions": completed_versions,
        "database_documents": database_documents,
        "database_versions": database_versions,
        "failed_versions": failed_versions,
        "embedding_generation_coverage": embedding_coverage,
        "idempotency_after_state_sha256": idempotency_after,
        "idempotency_before_state_sha256": idempotency_before,
        "idempotency_mismatch_count": idempotency_mismatches,
        "initial_load_transaction_timeout_seconds": (
            initial_load_transaction_timeout
        ),
        "interrupted_after_state_sha256": interrupted_after,
        "interrupted_before_state_sha256": interrupted_before,
        "interrupted_job_count": interrupted_job_count,
        "interrupted_task_node_count": interrupted_task_node_count,
        "primary_tenant_active_chunks": fixed_counts["primary_tenant_active_chunks"],
        "primary_visible_chunks": fixed_counts["primary_visible_chunks"],
        "recovered_job": recovered_job,
        "recovered_job_id": recovered_job_id,
        "recovered_job_linked_task_count": recovered_job_linked_task_count,
        "recovered_job_task_node_count": recovered_job_task_node_count,
        "recovery_checkpoint": recovery_checkpoint,
        "recovery_task_tracking_mode": task_tracking_mode,
        "replayed_active_versions": fixed_counts["replayed_active_versions"],
        "replay_completed_monotonic_ms": replay_completed,
        "replay_started_monotonic_ms": replay_started,
        "query_ready_monotonic_ms": query_ready,
        "schema_version": "production-ingestion-observation-v2",
        "started_monotonic_ms": started,
        "submitted_chunks": fixed_counts["submitted_chunks"],
        "total_active_chunks": fixed_counts["total_active_chunks"],
        "total_historical_chunks": fixed_counts["total_historical_chunks"],
        "total_versions": total_versions,
    }


def _validate_load_graph_state(value: Any) -> dict[str, Any]:
    evidence = _exact_mapping(
        value, _LOAD_GRAPH_STATE_FIELDS, "load graph-state evidence"
    )
    if evidence["schema_version"] != "canonical-graph-state-observation-v2":
        raise ValueError("load graph-state evidence schema is invalid")

    def snapshot(
        raw: Any,
        name: str,
        *,
        expected_shape: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = _exact_mapping(raw, _GRAPH_STATE_SNAPSHOT_FIELDS, name)
        labels_raw = state["label_counts"]
        if not isinstance(labels_raw, Mapping) or not labels_raw:
            raise ValueError(f"{name} label_counts must be a non-empty object")
        labels = {
            _text(label, f"{name} label"): _count(count, f"{name} {label} count")
            for label, count in labels_raw.items()
        }
        normalized = {
            "business_node_count": _count(
                state["business_node_count"], f"{name} node count", positive=True
            ),
            "business_relationship_count": _count(
                state["business_relationship_count"],
                f"{name} relationship count",
                positive=True,
            ),
            "label_counts": dict(sorted(labels.items())),
            "sha256": _digest(state["sha256"], f"{name} digest"),
        }
        if expected_shape is not None and {
            field: normalized[field] for field in expected_shape
        } != expected_shape:
            raise ValueError(
                f"{name} does not match its committed load-v1 graph shape"
            )
        return normalized

    before = snapshot(
        evidence["before_idempotent_replay"],
        "before idempotent replay state",
        expected_shape=_EXPECTED_PRE_GENERATION_LOAD_GRAPH_SHAPE,
    )
    after = snapshot(
        evidence["after_idempotent_replay"],
        "after idempotent replay state",
        expected_shape=_EXPECTED_PRE_GENERATION_LOAD_GRAPH_SHAPE,
    )
    query_ready = snapshot(
        evidence["query_ready_state"],
        "query-ready load state",
        expected_shape=_EXPECTED_LOAD_GRAPH_SHAPE,
    )
    mismatches = _count(
        evidence["idempotency_mismatch_count"], "graph-state mismatch count"
    )
    if mismatches != 0 or before != after:
        raise ValueError("load graph-state changed during idempotent replay")
    return {
        "after_idempotent_replay": after,
        "before_idempotent_replay": before,
        "idempotency_mismatch_count": mismatches,
        "query_ready_state": query_ready,
        "schema_version": "canonical-graph-state-observation-v2",
    }


def _validate_deletion_evidence(value: Any) -> dict[str, Any]:
    evidence = _exact_mapping(
        value,
        _DELETION_EVIDENCE_FIELDS,
        "deletion evidence",
    )
    if evidence["schema_version"] != "production-deletion-observation-v1":
        raise ValueError("deletion evidence schema is invalid")
    expected = _count_mapping(
        evidence["expected_removed_counts"],
        "deletion expected counts",
        _DELETION_LABELS,
    )
    observed = _count_mapping(
        evidence["observed_removed_counts"],
        "deletion observed counts",
        _DELETION_LABELS,
    )
    residue = _count_mapping(
        evidence["residue_by_label"],
        "deletion residue counts",
        _DELETION_LABELS,
    )
    residue_count = _count(
        evidence["deletion_residue_count"], "deletion_residue_count"
    )
    if expected != observed or residue_count != sum(residue.values()):
        raise ValueError("deletion counts do not prove complete removal")
    if expected != _EXPECTED_DELETION_COUNTS:
        raise ValueError("deletion counts do not match the fixed non-empty candidate")
    if (
        evidence["other_tenant_preserved"] is not True
        or evidence["durable_audit_records_retained"] is not True
    ):
        raise ValueError("deletion did not preserve isolation or durable audit state")
    preserved_tenants = _test_ids(
        evidence["preserved_tenant_ids"], "deletion preserved tenant IDs"
    )
    if (
        not _PRESERVED_LOAD_TENANTS <= set(preserved_tenants)
        or _DELETION_CANDIDATE["tenant_id"] in preserved_tenants
    ):
        raise ValueError("deletion evidence lacks all non-target load tenants")
    document_id = _text(evidence["document_id"], "deleted document_id")
    tenant_id = _text(evidence["tenant_id"], "deleted tenant_id")
    target_chunks = _test_ids(
        evidence["target_active_chunk_ids"], "deletion target active Chunk IDs"
    )
    target_snapshot = _text(
        evidence["target_active_snapshot_id"], "deletion target Snapshot ID"
    )
    target_version = _text(
        evidence["target_active_version_id"], "deletion target Version ID"
    )
    if {
        "active_chunk_ids": target_chunks,
        "active_snapshot_id": target_snapshot,
        "active_version_id": target_version,
        "document_id": document_id,
        "tenant_id": tenant_id,
    } != _DELETION_CANDIDATE:
        raise ValueError("deletion target does not match committed load-v1 candidate")
    delete_job_id = _text(evidence["delete_job_id"], "deletion job ID")
    audit_job_ids = _test_ids(
        evidence["durable_audit_job_ids"], "durable audit job IDs"
    )
    audit_job_count = _count(
        evidence["durable_audit_job_count"],
        "durable audit job count",
        positive=True,
    )
    audit_value = evidence["durable_audit_jobs"]
    if not isinstance(audit_value, list):
        raise ValueError("durable audit jobs must be a list")
    audit_jobs: list[dict[str, Any]] = []
    for index, raw_job in enumerate(audit_value):
        job = _exact_mapping(raw_job, _AUDIT_JOB_FIELDS, f"audit job {index}")
        targets: dict[str, str] = {}
        for field in ("target_snapshot_id", "target_version_id"):
            raw_target = job[field]
            if raw_target == "":
                targets[field] = ""
            else:
                targets[field] = _text(
                    raw_target, f"audit job {index} {field}"
                )
        audit_jobs.append(
            {
                "completed_tasks": _count(
                    job["completed_tasks"],
                    f"audit job {index} completed_tasks",
                ),
                "document_id": _text(
                    job["document_id"], f"audit job {index} document_id"
                ),
                "expected_tasks": _count(
                    job["expected_tasks"],
                    f"audit job {index} expected_tasks",
                ),
                "job_id": _text(job["job_id"], f"audit job {index} job_id"),
                "operation": _text(
                    job["operation"], f"audit job {index} operation"
                ),
                "operation_key": _text(
                    job["operation_key"], f"audit job {index} operation_key"
                ),
                "outcome": _text(
                    job["outcome"], f"audit job {index} outcome"
                ),
                "phase": _text(job["phase"], f"audit job {index} phase"),
                "status": _text(job["status"], f"audit job {index} status"),
                **targets,
                "tenant_id": _text(
                    job["tenant_id"], f"audit job {index} tenant_id"
                ),
            }
        )
    audit_jobs.sort(key=lambda item: item["job_id"])
    if (
        audit_job_count != 3
        or len(audit_jobs) != 3
        or len({item["job_id"] for item in audit_jobs}) != 3
        or audit_job_ids != [item["job_id"] for item in audit_jobs]
        or delete_job_id not in audit_job_ids
    ):
        raise ValueError("deletion requires exactly three bound durable audit jobs")
    expected_delete_job_id = ingestion_job_id(
        tenant_id, "DELETE", _DELETION_OPERATION_KEY
    )
    expected_jobs = [
        {
            "completed_tasks": 0,
            "document_id": document_id,
            "expected_tasks": 0,
            "job_id": expected_delete_job_id,
            "operation": "DELETE",
            "operation_key": _DELETION_OPERATION_KEY,
            "outcome": "DELETED",
            "phase": "COMPLETE",
            "status": "SUCCEEDED",
            "target_snapshot_id": "",
            "target_version_id": "",
            "tenant_id": tenant_id,
        }
    ]
    expected_jobs.extend(
        {
            "completed_tasks": 50,
            "document_id": document_id,
            "expected_tasks": 50,
            "job_id": ingestion_job_id(
                tenant_id, "INITIAL_LOAD", target["operation_key"]
            ),
            "operation": "INITIAL_LOAD",
            "operation_key": target["operation_key"],
            "outcome": target["outcome"],
            "phase": "COMPLETE",
            "status": "SUCCEEDED",
            "target_snapshot_id": target["snapshot_id"],
            "target_version_id": target["version_id"],
            "tenant_id": tenant_id,
        }
        for target in _INITIAL_LOAD_AUDIT_TARGETS
    )
    expected_jobs.sort(key=lambda item: item["job_id"])
    if delete_job_id != expected_delete_job_id or audit_jobs != expected_jobs:
        raise ValueError(
            "deletion durable audit jobs do not bind committed lifecycle operations"
        )
    tombstone = _count(
        evidence["tombstone_generation"],
        "deletion tombstone_generation",
        positive=True,
    )
    tombstone_job_id = _text(
        evidence["tombstone_deleted_by_job_id"],
        "deletion tombstone deleted_by_job_id",
    )
    if tombstone_job_id != delete_job_id:
        raise ValueError("deletion tombstone does not bind the DELETE job")
    return {
        "deletion_residue_count": residue_count,
        "delete_job_id": delete_job_id,
        "document_id": document_id,
        "durable_audit_job_count": audit_job_count,
        "durable_audit_job_ids": audit_job_ids,
        "durable_audit_jobs": audit_jobs,
        "durable_audit_records_retained": True,
        "expected_removed_counts": expected,
        "observed_removed_counts": observed,
        "other_tenant_preserved": True,
        "preserved_tenant_ids": preserved_tenants,
        "residue_by_label": residue,
        "schema_version": "production-deletion-observation-v1",
        "tenant_id": tenant_id,
        "tombstone_generation": tombstone,
        "tombstone_deleted_by_job_id": tombstone_job_id,
        "target_active_chunk_ids": target_chunks,
        "target_active_snapshot_id": target_snapshot,
        "target_active_version_id": target_version,
    }


def _validate_backup_evidence(value: Any) -> dict[str, Any]:
    evidence = _exact_mapping(value, _BACKUP_EVIDENCE_FIELDS, "backup evidence")
    if evidence["schema_version"] != "production-backup-restore-observation-v1":
        raise ValueError("backup evidence schema is invalid")
    database = _text(evidence["database"], "backup database")
    if database != "neo4j":
        raise ValueError("backup evidence must cover the configured neo4j database")
    dump_command = _text(evidence["dump_command"], "backup dump command")
    load_command = _text(evidence["load_command"], "backup load command")
    if dump_command != (
        "neo4j-admin database dump neo4j --to-path=/backups "
        "--overwrite-destination=true"
    ) or load_command != (
        "neo4j-admin database load neo4j --from-path=/backups "
        "--overwrite-destination=true"
    ):
        raise ValueError("backup evidence commands are not the reviewed dump/load cycle")
    source_digest = _digest(evidence["source_state_sha256"], "backup source digest")
    restored_digest = _digest(
        evidence["restored_state_sha256"], "backup restored digest"
    )
    source_resource_digest = _digest(
        evidence["source_container_resource_sha256"],
        "backup source container resource digest",
    )
    restored_resource_digest = _digest(
        evidence["restored_container_resource_sha256"],
        "backup restored container resource digest",
    )
    if (
        evidence["container_resources_match"] is not True
        or source_resource_digest != restored_resource_digest
    ):
        raise ValueError("backup source and restored resource envelopes differ")
    if evidence["schema_and_indexes_verified"] is not True:
        raise ValueError("restored schema and indexes were not verified")
    restored_node_count = _count(
        evidence["restored_business_node_count"],
        "restored node count",
        positive=True,
    )
    restored_relationship_count = _count(
        evidence["restored_business_relationship_count"],
        "restored relationship count",
        positive=True,
    )
    source_node_count = _count(
        evidence["source_business_node_count"],
        "source node count",
        positive=True,
    )
    source_relationship_count = _count(
        evidence["source_business_relationship_count"],
        "source relationship count",
        positive=True,
    )
    if (
        source_node_count != restored_node_count
        or source_relationship_count != restored_relationship_count
    ):
        raise ValueError("backup source and restored business graph counts differ")
    return {
        "backup_sha256": _digest(evidence["backup_sha256"], "backup artifact digest"),
        "backup_size_bytes": _count(
            evidence["backup_size_bytes"], "backup_size_bytes", positive=True
        ),
        "container_resources_match": True,
        "database": database,
        "dump_command": dump_command,
        "load_command": load_command,
        "restored_business_node_count": restored_node_count,
        "restored_business_relationship_count": restored_relationship_count,
        "restored_container_resource_sha256": restored_resource_digest,
        "restored_state_sha256": restored_digest,
        "schema_and_indexes_verified": True,
        "schema_version": "production-backup-restore-observation-v1",
        "source_business_node_count": source_node_count,
        "source_business_relationship_count": source_relationship_count,
        "source_container_resource_sha256": source_resource_digest,
        "source_state_sha256": source_digest,
    }


def _validate_evidence_manifest(
    value: Any,
    *,
    backup_dump_sha256: str,
    raw_artifacts: Mapping[str, Any],
    versions: Mapping[str, str],
    workload: Mapping[str, int | float | bool],
    request_record_count: int,
) -> dict[str, dict[str, int | str]]:
    manifest = _exact_mapping(value, _EVIDENCE_IDS, "evidence manifest")
    normalized: dict[str, dict[str, int | str]] = {}
    paths: set[str] = set()
    for evidence_id in sorted(_EVIDENCE_IDS):
        item = _exact_mapping(
            manifest[evidence_id],
            _EVIDENCE_FIELDS,
            f"evidence manifest {evidence_id}",
        )
        path = _relative_path(item["path"], f"evidence manifest {evidence_id} path")
        if path in paths:
            raise ValueError("evidence manifest paths must be unique")
        paths.add(path)
        digest = _digest(item["sha256"], f"evidence manifest {evidence_id} sha256")
        records = _count(
            item["record_count"],
            f"evidence manifest {evidence_id} record_count",
            positive=True,
        )
        schema = _text(item["schema"], f"evidence manifest {evidence_id} schema")
        if schema != _EVIDENCE_SCHEMAS[evidence_id]:
            raise ValueError(f"evidence manifest {evidence_id} schema is invalid")
        normalized[evidence_id] = {
            "path": path,
            "record_count": records,
            "schema": schema,
            "sha256": digest,
        }

    exact_counts = {
        "acceptance_contract": 1,
        "answer_embedding_corpus": 1,
        "backup_dump": 1,
        "backup_observation": 1,
        "container_inspection": 1,
        "fault_timeline": len(_SCENARIO_IDS),
        "graph_backup_source_state": 1,
        "graph_post_state": 1,
        "graph_pre_state": 1,
        "graph_restore_state": 1,
        "deletion_observation": 1,
        "ingestion_observation": 1,
        "large_database_quality": 1,
        "large_database_quality_cases": 49,
        "load_graph_state": 1,
        "production_configuration": 1,
        "provider_usage": 1,
        "reference_answer_predictions": 49,
        "request_samples": request_record_count,
        "retrieval_stage_samples": request_record_count,
        "stage8_report": 1,
        "suite_results": len(_SUITE_IDS),
        "validation_profile": 1,
    }
    for evidence_id, expected in exact_counts.items():
        if normalized[evidence_id]["record_count"] != expected:
            raise ValueError(
                f"evidence manifest {evidence_id} record_count does not match evidence"
            )
    if normalized["load_corpus"]["record_count"] < int(workload["chunk_count"]):
        raise ValueError("load corpus evidence does not cover the measured workload")

    digest_bindings = {
        "acceptance_contract": "contract_digest",
        "answer_embedding_corpus": "answer_embedding_corpus_digest",
        "load_corpus": "load_corpus_digest",
        "production_configuration": "configuration_digest",
        "reference_answer_predictions": "answer_prediction_digest",
        "stage8_report": "stage8_report_digest",
        "validation_profile": "profile_digest",
    }
    for evidence_id, version_field in digest_bindings.items():
        if normalized[evidence_id]["sha256"] != versions[version_field]:
            raise ValueError(
                f"evidence manifest {evidence_id} digest does not match versions"
            )
    expected_raw_ids = {
        "container_inspection",
        "backup_observation",
        "deletion_observation",
        "fault_timeline",
        "graph_backup_source_state",
        "graph_post_state",
        "graph_pre_state",
        "graph_restore_state",
        "ingestion_observation",
        "large_database_quality",
        "large_database_quality_cases",
        "load_graph_state",
        "provider_usage",
        "request_samples",
        "retrieval_stage_samples",
        "suite_results",
    }
    if set(raw_artifacts) != expected_raw_ids:
        raise AssertionError("internal raw evidence inventory is incomplete")
    for evidence_id, raw_artifact in raw_artifacts.items():
        calculated = f"sha256:{_canonical_digest(raw_artifact)}"
        if normalized[evidence_id]["sha256"] != calculated:
            raise ValueError(
                f"evidence manifest {evidence_id} digest does not bind raw observations"
            )
    if normalized["backup_dump"]["sha256"] != backup_dump_sha256:
        raise ValueError(
            "evidence manifest backup_dump digest does not bind backup evidence"
        )
    return normalized


def _validate_versions(value: Any) -> dict[str, str]:
    versions = _exact_mapping(value, _VERSION_FIELDS, "version inventory")
    normalized: dict[str, str] = {}
    for field in sorted(_VERSION_FIELDS):
        if field in _DIGEST_FIELDS:
            normalized[field] = _digest(versions[field], field)
        elif field == "code_commit":
            commit = _text(versions[field], field)
            if _COMMIT.fullmatch(commit) is None:
                raise ValueError("code_commit must be a full Git object ID")
            normalized[field] = commit.lower()
        else:
            normalized[field] = _text(versions[field], field)
    if normalized["contract_version"] != _CONTRACT_VERSION:
        raise ValueError("version inventory contract_version is stale")
    if normalized["profile_version"] != _PROFILE_VERSION:
        raise ValueError("version inventory profile_version is stale")
    if normalized["load_corpus_id"] != "load-v1":
        raise ValueError("version inventory must identify load-v1")
    if normalized["load_corpus_version"] != "1.0.2":
        raise ValueError("version inventory load_corpus_version is stale")
    if normalized["configuration_version"] != "1.0.5":
        raise ValueError("version inventory configuration_version is stale")
    reviewed_embedding_identities = {
        "answer_embedding_corpus_version": "1.0.1",
        "answer_embedding_model": "adjudicated-evidence-clusters",
        "answer_embedding_provider": "fixture",
        "answer_embedding_revision": "dev-corpus-v1.1",
        "answer_embedding_space_id": "19ef2d72-d978-5d0d-9f75-b7f33f9b6f4d",
        "embedding_model": "deterministic-load-sparse",
        "embedding_provider": "fixture",
        "embedding_revision": "load-v1.0",
        "embedding_space_id": "ef155576-e476-579d-9ce8-b6e0a233d0a9",
    }
    for field, expected in reviewed_embedding_identities.items():
        if normalized[field] != expected:
            raise ValueError(
                f"{field} does not match the reviewed workload embedding identity"
            )
    reviewed_prediction_identity = {
        "answer_prediction_digest": f"sha256:{REFERENCE_PREDICTION_SHA256}",
        "answer_prediction_provider": REFERENCE_PREDICTION_PROVIDER,
        "answer_prediction_version": REFERENCE_PREDICTION_VERSION,
    }
    for field, expected in reviewed_prediction_identity.items():
        if normalized[field] != expected:
            raise ValueError(f"{field} does not match the reviewed answer prediction")
    reviewed_component_identities = {
        "api_version": "0.1.0",
        "extractor_version": "synthetic-load-document-entity-extractor:v1",
        "governance_policy_version": "graph-governance-catalog-1.0.0",
        "graph_schema_version": "neo4j-migrations-001-through-005",
        "hardware_profile": "neo4j-8cpu-3072mb-loopback-v1",
        "index_version": "acl-partitioned-bm25-v2+exact-authorized-cosine-v1",
        "llm_model": "deterministic-grounded-answer",
        "llm_provider": "local-reference-llm",
        "llm_revision": "1.0.0",
        "neo4j_image": "neo4j:5.26.12-community",
        "neo4j_image_digest": (
            "sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37"
        ),
        "output_schema_version": "grounded-answer-output-v1.0.0",
        "prompt_version": "grounded-answer-v1.3.0",
        "splitter_version": "load-record-splitter:v1",
        "stage8_gold_digest": _REVIEWED_STAGE8_GOLD_MANIFEST_FILE_SHA256,
        "stage8_gold_version": "2.0.0",
    }
    for field, expected in reviewed_component_identities.items():
        if normalized[field] != expected:
            raise ValueError(
                f"{field} does not match the reviewed Stage 9 component identity"
            )
    reviewed_digests = {
        "answer_embedding_corpus_digest": (
            _REVIEWED_DEV_CORPUS_MANIFEST_FILE_SHA256
        ),
        "configuration_digest": _REVIEWED_CONFIGURATION_FILE_SHA256,
        "contract_digest": _REVIEWED_CONTRACT_FILE_SHA256,
        "load_corpus_digest": _REVIEWED_LOAD_MANIFEST_FILE_SHA256,
        "profile_digest": _REVIEWED_PROFILE_FILE_SHA256,
        "stage8_report_semantic_digest": (
            _REVIEWED_STAGE8_REPORT_SEMANTIC_SHA256
        ),
    }
    for field, expected in reviewed_digests.items():
        if normalized[field] != expected:
            raise ValueError(f"{field} does not match the reviewed Stage 9 input")
    return normalized


def _validate_metrics(value: Any) -> dict[str, int | float]:
    metrics = _exact_mapping(value, frozenset(_THRESHOLDS), "metric observations")
    normalized: dict[str, int | float] = {}
    for metric_id in sorted(_THRESHOLDS):
        raw = metrics[metric_id]
        if metric_id in _COUNT_METRICS:
            normalized[metric_id] = _count(raw, metric_id)
            continue
        number = _finite(raw, metric_id)
        if metric_id in _RATIO_METRICS and number > 1:
            raise ValueError(f"{metric_id} must be between zero and one")
        normalized[metric_id] = number
    return normalized


def _same_number(left: int | float, right: int | float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-9)


def _metric_rows(
    definitions: Mapping[str, Mapping[str, Any]],
    metrics: Mapping[str, int | float],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for metric_id in sorted(_THRESHOLDS):
        definition = definitions[metric_id]
        observed = metrics[metric_id]
        passed = compare(definition["operator"], observed, definition["target"])
        rows.append(
            {
                "area": definition["area"],
                "id": metric_id,
                "observed": observed,
                "operator": definition["operator"],
                "passed": passed,
                "target": definition["target"],
                "unit": definition["unit"],
            }
        )
        if not passed:
            failures.append(
                f"{metric_id}: {observed} {definition['operator']} "
                f"{definition['target']} failed"
            )
    return rows, failures


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_production_candidate_report(
    observations: Mapping[str, Any],
    contract: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one production-reference observation set and apply all gates.

    Structurally invalid, incomplete, internally inconsistent, non-finite, or
    caller-qualified observations raise :class:`ValueError`.  Valid evidence
    that fails an acceptance threshold or required scenario produces a report
    with ``passed`` and ``production_candidate_eligible`` set to ``False``.
    """

    if not isinstance(observations, Mapping):
        raise ValueError("production observations must be an object")
    _reject_nested_eligibility(observations)
    forged = sorted(_FORGED_QUALIFICATION_FIELDS & set(observations))
    if forged:
        raise ValueError(f"qualification fields are output-only: {forged}")
    observations = _exact_mapping(
        observations, _OBSERVATION_FIELDS, "production observations"
    )
    if observations["schema_version"] != PRODUCTION_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("production observation schema version is invalid")
    if observations["profile_id"] != _PROFILE_ID:
        raise ValueError("production observations must use production-reference")

    definitions = _validate_contract(contract)
    _validate_profile(profile)
    workload = _validate_workload(observations["workload"])
    ingestion_evidence = _validate_ingestion_evidence(
        observations["ingestion_evidence"]
    )
    load_graph_state = _validate_load_graph_state(observations["load_graph_state"])
    if (
        load_graph_state["idempotency_mismatch_count"]
        != ingestion_evidence["idempotency_mismatch_count"]
        or load_graph_state["before_idempotent_replay"]["sha256"]
        != ingestion_evidence["idempotency_before_state_sha256"]
        or load_graph_state["after_idempotent_replay"]["sha256"]
        != ingestion_evidence["idempotency_after_state_sha256"]
    ):
        raise ValueError("ingestion evidence does not bind load graph-state evidence")
    deletion_evidence = _validate_deletion_evidence(
        observations["deletion_evidence"]
    )
    backup_evidence = _validate_backup_evidence(observations["backup_evidence"])
    (
        request_samples,
        request_percentiles,
        status_counts,
        measured_duration,
        retrieval_rps,
        error_rate,
        concurrency_diagnostics,
        request_failures,
        http_answer_metrics,
    ) = _validate_request_samples(
        observations["request_samples"],
        answer_samples=int(workload["answer_samples"]),
        concurrency=int(workload["concurrency"]),
        declared_duration_seconds=float(workload["sustained_seconds"]),
    )
    traffic, ingestion_rps = _validate_traffic(
        observations["traffic"], workload
    )
    percentiles = _validate_latencies(observations["latency_ms"])
    percentiles.update(request_percentiles)
    retrieval_stage_samples = sorted(
        (
            {
                "request_id": item["request_id"],
                "retrieval_stage_ms": item["retrieval_stage_ms"],
                "trace_id": item["trace_id"],
            }
            for samples in request_samples.values()
            for item in samples
        ),
        key=lambda item: item["request_id"],
    )
    if any(item["trace_id"] is None for item in retrieval_stage_samples):
        raise ValueError("retrieval-stage samples require trace identities")
    declared_retrieval_stages = sorted(
        _finite(item, "retrieval-stage latency observation")
        for item in observations["latency_ms"]["retrieval_stage_ms"]
    )
    recorded_retrieval_stages = sorted(
        float(item["retrieval_stage_ms"]) for item in retrieval_stage_samples
    )
    if declared_retrieval_stages != recorded_retrieval_stages:
        raise ValueError(
            "retrieval-stage latency observations must exactly cover measured requests"
        )
    provider_evidence = _validate_provider_evidence(observations["provider_evidence"])
    if percentiles["embedding_provider"]["sample_count"] != provider_evidence[
        "measured_embedding_model_calls"
    ]:
        raise ValueError("embedding provider latency samples do not cover model calls")
    if percentiles["llm"]["sample_count"] != provider_evidence[
        "measured_answer_model_calls"
    ]:
        raise ValueError("LLM latency samples do not cover model calls")
    cost = _validate_cost(
        observations["cost"],
        answer_samples=len(request_samples["answer"]),
        retrieval_samples=len(request_samples["retrieval"]),
        provider_mode=str(provider_evidence["mode"]),
    )
    expected_embedding_calls = sum(len(items) for items in request_samples.values())
    if provider_evidence["measured_embedding_model_calls"] != expected_embedding_calls:
        raise ValueError("embedding model calls do not match measured requests")
    if provider_evidence["measured_answer_model_calls"] != len(
        request_samples["answer"]
    ):
        raise ValueError("answer model calls do not match measured answer requests")
    provider_failures = (
        []
        if provider_evidence["peak_concurrency"] >= int(workload["concurrency"])
        else ["measured provider peak concurrency did not reach eight clients"]
    )
    fault_timeline = _validate_fault_timeline(observations["fault_timeline"])
    idempotency_event = fault_timeline["idempotency"]
    if (
        not _same_number(
            idempotency_event["started_monotonic_ms"],
            ingestion_evidence["replay_started_monotonic_ms"],
        )
        or not _same_number(
            idempotency_event["completed_monotonic_ms"],
            ingestion_evidence["replay_completed_monotonic_ms"],
        )
    ):
        raise ValueError(
            "idempotency fault timeline does not bind ingestion replay evidence"
        )
    first_measured_request = min(
        float(sample["started_monotonic_ms"])
        for samples in request_samples.values()
        for sample in samples
    )
    if ingestion_evidence["query_ready_monotonic_ms"] >= first_measured_request:
        raise ValueError(
            "query-ready evidence must precede every measured HTTP request"
        )
    scenarios, scenario_failures = _validate_scenarios(
        observations["scenarios"], fault_timeline
    )
    suites, suite_failures = _validate_suite_results(observations["suite_results"])
    canonical_graph, graph_failures = _validate_canonical_graph(
        observations["canonical_graph"]
    )
    load_snapshot = load_graph_state["query_ready_state"]
    validation_snapshot = canonical_graph["pre_validation_state"]
    if (
        validation_snapshot["business_node_count"]
        < load_snapshot["business_node_count"]
        or validation_snapshot["business_relationship_count"]
        < load_snapshot["business_relationship_count"]
        or any(
            validation_snapshot["label_counts"].get(label, 0) < count
            for label, count in load_snapshot["label_counts"].items()
        )
    ):
        raise ValueError(
            "pre-validation graph does not preserve the committed load-v1 shape"
        )
    versions = _validate_versions(observations["versions"])
    environment, environment_failures = _validate_runtime_environment(
        observations["runtime_environment"], versions
    )
    quality_evidence, quality_failures = _validate_quality_evidence(
        observations["quality_evidence"]
    )
    evidence_manifest = _validate_evidence_manifest(
        observations["evidence_manifest"],
        backup_dump_sha256=backup_evidence["backup_sha256"],
        raw_artifacts={
            "backup_observation": backup_evidence,
            "container_inspection": environment,
            "deletion_observation": deletion_evidence,
            "fault_timeline": fault_timeline,
            "graph_backup_source_state": canonical_graph[
                "backup_source_state"
            ],
            "graph_post_state": canonical_graph["post_validation_state"],
            "graph_pre_state": canonical_graph["pre_validation_state"],
            "graph_restore_state": canonical_graph["restored_state"],
            "ingestion_observation": ingestion_evidence,
            "load_graph_state": load_graph_state,
            "large_database_quality": quality_evidence,
            "large_database_quality_cases": quality_evidence["case_evidence"],
            "provider_usage": {
                "cost": cost,
                "embedding_latency_ms": sorted(
                    _finite(item, "embedding provider evidence latency")
                    for item in observations["latency_ms"]["embedding_provider"]
                ),
                "llm_latency_ms": sorted(
                    _finite(item, "llm provider evidence latency")
                    for item in observations["latency_ms"]["llm"]
                ),
                "provider_evidence": provider_evidence,
            },
            "request_samples": request_samples,
            "retrieval_stage_samples": retrieval_stage_samples,
            "suite_results": suites,
        },
        versions=versions,
        workload=workload,
        request_record_count=sum(len(items) for items in request_samples.values()),
    )
    metrics = _validate_metrics(observations["metrics"])
    if any(
        not _same_number(metrics[metric_id], expected)
        for metric_id, expected in _REVIEWED_STAGE8_GRAPH_METRICS.items()
    ):
        raise ValueError(
            "graph metrics do not match the reviewed Stage 8 semantic baseline"
        )

    if (
        not _same_number(
            traffic["ingestion_started_monotonic_ms"],
            ingestion_evidence["started_monotonic_ms"],
        )
        or not _same_number(
            traffic["ingestion_completed_monotonic_ms"],
            ingestion_evidence["completed_monotonic_ms"],
        )
        or traffic["ingestion_chunks"] != ingestion_evidence["submitted_chunks"]
    ):
        raise ValueError("ingestion traffic does not bind ingestion evidence")
    ingestion_rate = ingestion_evidence["completed_versions"] / ingestion_evidence[
        "total_versions"
    ]
    lifecycle_cross_checks = {
        "ingestion_success_rate": ingestion_rate,
        "idempotency_mismatch_count": ingestion_evidence[
            "idempotency_mismatch_count"
        ],
        "deletion_residue_count": deletion_evidence["deletion_residue_count"],
    }
    for metric_id, measured in lifecycle_cross_checks.items():
        if not _same_number(metrics[metric_id], measured):
            raise ValueError(f"{metric_id} does not match lifecycle evidence")
    backup_source_state = canonical_graph["backup_source_state"]
    restored_state = canonical_graph["restored_state"]
    if (
        backup_evidence["source_state_sha256"] != backup_source_state["sha256"]
        or backup_evidence["restored_state_sha256"] != restored_state["sha256"]
        or backup_evidence["source_business_node_count"]
        != backup_source_state["business_node_count"]
        or backup_evidence["source_business_relationship_count"]
        != backup_source_state["business_relationship_count"]
        or backup_evidence["restored_business_node_count"]
        != restored_state["business_node_count"]
        or backup_evidence["restored_business_relationship_count"]
        != restored_state["business_relationship_count"]
    ):
        raise ValueError("backup evidence does not bind canonical graph states")
    if quality_evidence["graph_state_sha256"] != canonical_graph[
        "pre_validation_state"
    ]["sha256"]:
        raise ValueError(
            "large-database quality evidence does not bind its canonical graph state"
        )
    quality_prediction_identity = {
        "prediction_provider": quality_evidence["prediction_provider"],
        "prediction_sha256": quality_evidence["prediction_sha256"],
        "prediction_version": quality_evidence["prediction_version"],
    }
    version_prediction_identity = {
        "prediction_provider": versions["answer_prediction_provider"],
        "prediction_sha256": versions["answer_prediction_digest"],
        "prediction_version": versions["answer_prediction_version"],
    }
    if quality_prediction_identity != version_prediction_identity:
        raise ValueError(
            "large-database quality evidence does not bind the versioned answer prediction"
        )

    retrieval_quality = quality_evidence["retrieval_metrics"]
    answer_quality = quality_evidence["answer_metrics"]
    quality_cross_checks = {
        "recall_at_5": retrieval_quality["recall_at_5"],
        "mrr": retrieval_quality["mrr"],
        "ndcg_at_5": retrieval_quality["ndcg_at_5"],
        "unauthorized_exposure_count": retrieval_quality[
            "unauthorized_exposure_count"
        ],
        "supported_claim_rate": answer_quality["supported_claim_rate"],
        "citation_precision": answer_quality["citation_precision"],
        "citation_coverage": answer_quality["citation_coverage"],
        "numerical_fidelity": answer_quality["numerical_fidelity"],
        "refusal_f1": answer_quality["refusal_f1"],
    }
    for metric_id, measured in quality_cross_checks.items():
        if not _same_number(metrics[metric_id], measured):
            raise ValueError(
                f"{metric_id} does not match large-database quality evidence"
            )

    cross_checks = {
        "retrieval_p95_ms": nearest_rank_percentile(
            [
                float(item["retrieval_stage_ms"])
                for item in request_samples["retrieval"]
            ],
            0.95,
        ),
        "answer_p95_ms": percentiles["answer"]["p95"],
        "retrieval_throughput_rps": retrieval_rps,
        "server_error_rate": error_rate,
    }
    for metric_id, independently_calculated in cross_checks.items():
        if not _same_number(metrics[metric_id], independently_calculated):
            raise ValueError(
                f"{metric_id} does not match independently calculated observations"
            )

    limitations = _notes(
        observations["limitations"], "limitations", required=True
    )
    residual_risks = _notes(
        observations["residual_risks"], "residual_risks", required=True
    )
    prerequisites = _notes(
        observations["deployment_prerequisites"],
        "deployment_prerequisites",
        required=True,
    )
    missing_prerequisites = sorted(
        set(REQUIRED_DEPLOYMENT_PREREQUISITES) - set(prerequisites)
    )
    if missing_prerequisites:
        raise ValueError(
            "deployment_prerequisites are missing required release boundaries: "
            + "; ".join(missing_prerequisites)
        )
    if provider_evidence["mode"] == "deterministic_reference":
        limitations = sorted(set(limitations) | {_DETERMINISTIC_PROVIDER_LIMITATION})
        prerequisites = sorted(
            set(prerequisites) | {_EXTERNAL_PROVIDER_PREREQUISITE}
        )
    metric_rows, metric_failures = _metric_rows(definitions, metrics)
    failures = (
        metric_failures
        + scenario_failures
        + suite_failures
        + graph_failures
        + environment_failures
        + quality_failures
        + provider_failures
    )
    if request_failures:
        failures.append(
            f"{len(request_failures)} measured request(s) failed semantic validation"
        )
    failures = sorted(set(failures))
    passed = not failures

    report: dict[str, Any] = {
        "backup_evidence": backup_evidence,
        "canonical_graph": canonical_graph,
        "contract_metrics": metric_rows,
        "deployment_prerequisites": prerequisites,
        "deletion_evidence": deletion_evidence,
        "evidence_manifest": evidence_manifest,
        "failures": failures,
        "fault_timeline": fault_timeline,
        "latency_percentiles_ms": percentiles,
        "ingestion_evidence": ingestion_evidence,
        "load_graph_state": load_graph_state,
        "limitations": limitations,
        "operating_cost": cost,
        "passed": passed,
        "production_candidate_eligible": passed,
        "profile_id": _PROFILE_ID,
        "provider_evidence": provider_evidence,
        "quality_evidence": quality_evidence,
        "request_diagnostics": {
            "answer_sample_count": len(request_samples["answer"]),
            "http_answer_quality": http_answer_metrics,
            "measured_duration_seconds": measured_duration,
            **concurrency_diagnostics,
            "retrieval_client_count": len(
                {item["client_id"] for item in request_samples["retrieval"]}
            ),
            "retrieval_sample_count": len(request_samples["retrieval"]),
            "retrieval_stage_sample_count": len(retrieval_stage_samples),
            "semantic_failure_count": len(request_failures),
            "status_counts": status_counts,
        },
        "residual_risks": residual_risks,
        "runtime_environment": environment,
        "scenarios": scenarios,
        "schema_version": PRODUCTION_REPORT_SCHEMA_VERSION,
        "suite_results": suites,
        "throughput": {
            "ingestion_chunks_per_second": ingestion_rps,
            "retrieval_requests_per_second": retrieval_rps,
            "server_error_rate": error_rate,
        },
        "traffic": traffic,
        "versions": versions,
        "workload": workload,
    }
    report["semantic_digest"] = _canonical_digest(report)
    return report


__all__ = [
    "PRODUCTION_OBSERVATION_SCHEMA_VERSION",
    "PRODUCTION_REPORT_SCHEMA_VERSION",
    "build_production_candidate_report",
]
