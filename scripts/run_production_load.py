#!/usr/bin/env python3
"""Run authenticated Stage 9 HTTP load and deterministic dependency faults."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
import jwt
import neo4j
import uvicorn

from graphrag_prod.api.app import APISettings, create_app
from graphrag_prod.api.auth import JWTAuthConfig, JWTAuthenticator
from graphrag_prod.api.backend import (
    GeneratedAnswer,
    GraphRAGApplicationBackend,
    GraphRAGQueryOperations,
    ProviderUsage,
    QueryEmbedding,
)
from graphrag_prod.api.contracts import ReadinessResponse
from graphrag_prod.api.runtime import (
    Backend,
    BackendResult,
    OperationEnvelope,
    OperationKind,
    RateLimitPolicy,
    RuntimePolicy,
)
from graphrag_prod.evaluation.quality_evidence import build_http_answer_commitment
from graphrag_prod.evaluation.production_config import (
    resolve_production_answer_retrieval_limits,
)
from graphrag_prod.evaluation.reference_predictions import (
    load_reference_predictions,
    prediction_payload,
)
from graphrag_prod.generation import (
    AnswerModelRequest,
    GenerationRequest,
    GroundedGenerationService,
)
from graphrag_prod.retrieval import (
    Neo4jRetrievalEngine,
)
from scripts.build_load_corpus import (
    EMBEDDING_SPACE_ID,
    PRIMARY_TENANT_ID,
    RETRIEVAL_ANCHOR_SELECTION,
    RETRIEVAL_MINIMUM_ANCHOR_COSINE,
    build_manifest,
    deterministic_vector,
    iter_chunks,
)


_ISSUER = "urn:sample-graphrag:stage9:identity"
_AUDIENCE = "sample-graphrag-stage9"
_GOLD_DATASET_ID = "gold-v1"
_LOAD_DATASET_ID = "load-v1"
_PROVIDER_TIMEOUT_IDS = ("embedding_provider", "llm_provider")
_ACCESS_EVIDENCE_SCHEMA_VERSION = "load-v1-access-isolation-v1"
_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE_PREDICTIONS = _ROOT / "evaluation" / "reference-answer-predictions.v1.json"
_GOLD_QUESTIONS = _ROOT / "evaluation" / "gold-v1" / "questions.jsonl"
_DEV_CORPUS_MANIFEST = _ROOT / "datasets" / "dev-corpus-v1" / "manifest.json"
_DEV_CORPUS_VECTORS = _ROOT / "datasets" / "dev-corpus-v1" / "vectors.jsonl"


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _text_sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RuntimeError(f"{path}:{line_number} must contain an object")
            rows.append(value)
    return rows


def _vector_checksum(vector: Iterable[float]) -> str:
    return hashlib.sha256(
        json.dumps(
            list(vector),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _provider_timeout_window_ms(
    config: dict[str, Any],
    provider_id: str,
) -> tuple[float, float]:
    """Return the allowed API-observed timeout window for one slow provider."""

    if provider_id not in _PROVIDER_TIMEOUT_IDS:
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


class _NullLogger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        return None

    def warning(self, *_args: object, **_kwargs: object) -> None:
        return None

    def error(self, *_args: object, **_kwargs: object) -> None:
        return None


class _UnavailableDocuments:
    def ingest(self, *_args: object, **_kwargs: object) -> BackendResult:
        raise RuntimeError("document writes are outside the retrieval load boundary")

    def delete(self, *_args: object, **_kwargs: object) -> BackendResult:
        raise RuntimeError("document writes are outside the retrieval load boundary")

    def get_job(self, *_args: object, **_kwargs: object) -> BackendResult:
        raise RuntimeError("job reads are outside the retrieval load boundary")


class _RetrievalStageRecorder:
    """Capture the backend's measured retrieval stage by HTTP request identity.

    ``GraphRAGQueryOperations`` measures this stage around the retrieval-engine
    call.  It therefore includes the Neo4j queries plus in-process ranking and
    hydration, and is deliberately not labelled as database wire latency.
    """

    def __init__(self, delegate: Backend) -> None:
        self._delegate = delegate
        self._lock = threading.Lock()
        self._samples: dict[str, dict[str, Any]] = {}

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        result = self._delegate.execute(envelope)
        if envelope.operation not in {OperationKind.RETRIEVAL, OperationKind.ANSWER}:
            return result
        stage_values = [
            float(duration)
            for name, duration in result.usage.stages
            if name == "retrieval"
        ]
        if len(stage_values) != 1 or stage_values[0] != result.usage.retrieval_ms:
            raise RuntimeError("retrieval-stage usage metadata is inconsistent")
        sample = {
            "request_id": envelope.request_id,
            "retrieval_stage_ms": stage_values[0],
            "trace_id": envelope.trace_id,
        }
        with self._lock:
            if envelope.request_id in self._samples:
                raise RuntimeError("duplicate retrieval-stage request identity")
            self._samples[envelope.request_id] = sample
        return result

    def samples_for(self, request_ids: set[str]) -> list[dict[str, Any]]:
        with self._lock:
            missing = request_ids - set(self._samples)
            selected = [
                dict(self._samples[request_id]) for request_id in sorted(request_ids)
                if request_id in self._samples
            ]
        if missing:
            raise RuntimeError(
                "retrieval-stage evidence is missing measured requests: "
                f"{sorted(missing)[:5]}"
            )
        return selected


class _Readiness:
    def __init__(
        self,
        driver: neo4j.Driver,
        database: str,
        *,
        transaction_timeout_seconds: float,
    ) -> None:
        if (
            isinstance(transaction_timeout_seconds, bool)
            or not isinstance(transaction_timeout_seconds, (int, float))
            or not math.isfinite(float(transaction_timeout_seconds))
            or not 0 < float(transaction_timeout_seconds) <= 300
        ):
            raise ValueError(
                "transaction_timeout_seconds must be a finite number "
                "between 0 and 300"
            )
        self.driver = driver
        self.database = database
        self.transaction_timeout_seconds = float(transaction_timeout_seconds)
        self.query = neo4j.Query(
            "RETURN 1 AS ready",
            metadata={
                "component": "graphrag-readiness",
                "operation": "readiness",
            },
            timeout=self.transaction_timeout_seconds,
        )

    def check(self) -> BackendResult:
        records, _, _ = self.driver.execute_query(
            self.query,
            database_=self.database,
            routing_=neo4j.RoutingControl.READ,
        )
        if len(records) != 1 or int(records[0]["ready"]) != 1:
            return BackendResult(
                ReadinessResponse(status="not_ready", checks={"neo4j": "error"})
            )
        return BackendResult(
            ReadinessResponse(status="ready", checks={"neo4j": "ok"})
        )


class _SlowNeo4jRetrievalEngine(Neo4jRetrievalEngine):
    """Force real server work so the transaction deadline is observable."""

    @staticmethod
    def _retrieve_tx(tx: Any, _request: Any) -> Any:
        return tx.run(
            "UNWIND range(1, 1000000000) AS value "
            "WITH value WHERE value % 2 = 0 RETURN count(value) AS count"
        ).single()


class _InvalidNeo4jRetrievalEngine(Neo4jRetrievalEngine):
    """Exercise deterministic server-error classification with real Cypher."""

    @staticmethod
    def _retrieve_tx(tx: Any, _request: Any) -> Any:
        return tx.run("THIS IS NOT CYPHER").single()


class _SwitchableRetrievalEngine:
    """Select one real retrieval dependency for sequential fault probes."""

    def __init__(self, delegate: Neo4jRetrievalEngine) -> None:
        self._delegate = delegate
        self._lock = threading.Lock()

    def set_delegate(self, delegate: Neo4jRetrievalEngine) -> None:
        with self._lock:
            self._delegate = delegate

    def retrieve(self, request: Any) -> Any:
        with self._lock:
            delegate = self._delegate
        return delegate.retrieve(request)


class _ReferenceQueryEmbedder:
    def __init__(
        self,
        vectors: dict[tuple[str, str], tuple[tuple[float, ...], str]],
        *,
        delay_ms: float,
        timeout_delay_ms: float,
    ) -> None:
        self.vectors = vectors
        self.delay_ms = delay_ms
        self.timeout_delay_ms = timeout_delay_ms
        self._mode = "success"
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self.calls = 0
        self.successful_calls = 0
        self.latencies_ms: list[float] = []
        self.active_calls = 0
        self.peak_concurrency = 0

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        with self._idle:
            return self._idle.wait_for(
                lambda: self.active_calls == 0,
                timeout=timeout_seconds,
            )

    def embed(self, query_text: str, *, tenant_id: str) -> QueryEmbedding:
        with self._lock:
            mode = self._mode
            self.calls += 1
            self.active_calls += 1
            self.peak_concurrency = max(self.peak_concurrency, self.active_calls)
        try:
            if mode == "timeout":
                # Deliberately finish successfully after the API deadline.  A
                # 504 therefore proves RuntimePolicy enforcement, not a
                # provider stub raising its own TimeoutError.
                time.sleep(self.timeout_delay_ms / 1_000.0)
            if mode in {"unavailable", "failure"}:
                raise RuntimeError("injected embedding provider failure")
            started = time.monotonic_ns()
            time.sleep(self.delay_ms / 1_000.0)
            query = self.vectors.get((tenant_id, query_text))
            if query is None:
                raise RuntimeError("reference query has no versioned vector")
            vector, embedding_space_id = query
            completed = time.monotonic_ns()
            with self._lock:
                self.successful_calls += 1
                self.latencies_ms.append((completed - started) / 1_000_000)
            return QueryEmbedding(
                vector,
                embedding_space_id,
                ProviderUsage(input_tokens=12, model_calls=1),
            )
        finally:
            with self._idle:
                self.active_calls -= 1
                self._idle.notify_all()


class _ReferenceAnswerModel:
    def __init__(
        self,
        *,
        delay_ms: float,
        timeout_delay_ms: float,
        predictions_by_query: dict[str, dict[str, Any]],
    ) -> None:
        self.delay_ms = delay_ms
        self.timeout_delay_ms = timeout_delay_ms
        self.predictions_by_query = predictions_by_query
        self._mode = "success"
        self._lock = threading.Lock()
        self.latencies_ms: list[float] = []

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode

    def generate(self, request: AnswerModelRequest) -> object:
        with self._lock:
            mode = self._mode
        started = time.monotonic_ns()
        time.sleep(self.delay_ms / 1_000.0)
        completed = time.monotonic_ns()
        with self._lock:
            self.latencies_ms.append((completed - started) / 1_000_000)
        if mode == "malformed":
            return {"status": "answered", "claims": "unsafe", "conflicts": []}
        return prediction_payload(request, self.predictions_by_query)


class _MeteredReferenceGeneration:
    def __init__(
        self,
        model: _ReferenceAnswerModel,
        *,
        input_tokens: int,
        output_tokens: int,
        request_cost_usd: float,
    ) -> None:
        self.model = model
        self.service = GroundedGenerationService(model)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.request_cost_usd = request_cost_usd
        self._mode = "success"
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self.calls = 0
        self.metered_calls = 0
        self.active_calls = 0

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode
        self.model.set_mode("malformed" if mode == "failure" else "success")

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        with self._idle:
            return self._idle.wait_for(
                lambda: self.active_calls == 0,
                timeout=timeout_seconds,
            )

    def generate(self, request: GenerationRequest):
        return self.generate_with_usage(request).answer

    def generate_with_usage(self, request: GenerationRequest) -> GeneratedAnswer:
        with self._idle:
            mode = self._mode
            self.calls += 1
            self.active_calls += 1
        try:
            if mode == "timeout":
                # As above, the delayed provider eventually succeeds; only the
                # API runtime is allowed to create the timeout response.
                time.sleep(self.model.timeout_delay_ms / 1_000.0)
            if mode == "unavailable":
                raise RuntimeError("injected answer provider unavailable")
            result = self.service.generate(request)
            with self._lock:
                self.metered_calls += 1
            return GeneratedAnswer(
                result,
                ProviderUsage(
                    input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens,
                    model_calls=1,
                    estimated_cost_usd=self.request_cost_usd,
                ),
            )
        finally:
            with self._idle:
                self.active_calls -= 1
                self._idle.notify_all()


@dataclass(frozen=True, slots=True)
class _ReferenceServer:
    server: uvicorn.Server
    thread: threading.Thread
    embedder: _ReferenceQueryEmbedder
    generation: _MeteredReferenceGeneration
    driver: neo4j.Driver
    retrieval: _SwitchableRetrievalEngine
    retrieval_stages: _RetrievalStageRecorder
    retrieval_transaction_timeout_seconds: float
    readiness_transaction_timeout_seconds: float
    readiness_probe_status: str


def _load_settings() -> tuple[str, str, str, str]:
    names = (
        "TEST_NEO4J_URI",
        "TEST_NEO4J_USER",
        "TEST_NEO4J_PASSWORD",
        "TEST_NEO4J_DATABASE",
    )
    values = tuple(os.getenv(name, "") for name in names)
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
    if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
        raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
    host = urlparse(values[0]).hostname
    if host is None or not ipaddress.ip_address(host).is_loopback:
        raise RuntimeError("Stage 9 accepts only a loopback disposable Neo4j URI")
    return values  # type: ignore[return-value]


def _probe_reference_readiness(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url}/health/ready", timeout=1)
        if response.status_code != 200:
            return False
        return response.json() == {
            "status": "ready",
            "checks": {"neo4j": "ok"},
        }
    except (httpx.HTTPError, ValueError):
        return False


def _token(
    secret: bytes,
    subject: str,
    groups: Iterable[str],
    *,
    tenant_id: str,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "aud": _AUDIENCE,
            "exp": now + 1_800,
            "groups": sorted(groups),
            "iat": now,
            "iss": _ISSUER,
            "scope": "retrieval:read answers:generate",
            "sub": subject,
            "tenant_id": tenant_id,
        },
        secret,
        algorithm="HS256",
        headers={"typ": "JWT"},
    )


def _available_port(requested: int) -> int:
    if requested:
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _queries() -> tuple[dict[str, Any], ...]:
    manifest = build_manifest()
    workload = manifest.get("retrieval_workload")
    if not isinstance(workload, dict) or set(workload) != {
        "anchor_selection",
        "dataset_id",
        "minimum_anchor_cosine",
        "principal",
        "queries",
        "query_count",
        "schema_version",
        "vector_derivation",
    }:
        raise RuntimeError("load-v1 retrieval workload schema is invalid")
    principal = workload["principal"]
    expected_groups = manifest["coverage"]["load_principal_groups"]
    if (
        workload["dataset_id"] != _LOAD_DATASET_ID
        or workload["anchor_selection"] != RETRIEVAL_ANCHOR_SELECTION
        or workload["minimum_anchor_cosine"]
        != RETRIEVAL_MINIMUM_ANCHOR_COSINE
        or workload["schema_version"] != "load-retrieval-workload-v1"
        or workload["vector_derivation"]
        != manifest["embedding_profile"]["derivation"]
        or principal != {
            "groups": expected_groups,
            "tenant_id": PRIMARY_TENANT_ID,
        }
    ):
        raise RuntimeError("load-v1 retrieval workload identity is invalid")
    raw_queries = workload["queries"]
    if (
        not isinstance(raw_queries, list)
        or workload["query_count"] != 64
        or len(raw_queries) != 64
    ):
        raise RuntimeError("load-v1 retrieval workload must contain 64 queries")
    chunks_by_id = {
        item["chunk_id"]: item
        for item in iter_chunks(tenant_id=PRIMARY_TENANT_ID, active_only=True)
    }
    queries: list[dict[str, Any]] = []
    for index, item in enumerate(raw_queries):
        if not isinstance(item, dict) or set(item) != {
            "case_id",
            "embedding_space_id",
            "expected_chunk_ids",
            "expected_version_id",
            "query_text",
            "query_vector_checksum",
            "tenant_id",
        }:
            raise RuntimeError("load-v1 retrieval query schema is invalid")
        expected_chunk_ids = item["expected_chunk_ids"]
        case_id = f"load-anchor-{index:02d}"
        if (
            item["case_id"] != case_id
            or item["tenant_id"] != PRIMARY_TENANT_ID
            or item["embedding_space_id"] != EMBEDDING_SPACE_ID
            or not isinstance(expected_chunk_ids, list)
            or len(expected_chunk_ids) != 1
        ):
            raise RuntimeError(f"load-v1 retrieval query identity is invalid: {case_id}")
        chunk_id = expected_chunk_ids[0]
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None or item["expected_version_id"] != chunk["version_id"]:
            raise RuntimeError(f"load-v1 query provenance is invalid: {case_id}")
        vector = deterministic_vector(chunk_id)
        checksum = _vector_checksum(vector)
        if checksum != item["query_vector_checksum"]:
            raise RuntimeError(f"load-v1 query vector checksum drifted: {case_id}")
        if item["query_text"] != chunk["text"].rstrip("\n"):
            raise RuntimeError(
                f"load-v1 query text is not bound to its source Chunk: {case_id}"
            )
        queries.append(
            {
                "case_id": item["case_id"],
                "dataset_id": workload["dataset_id"],
                "embedding_space_id": item["embedding_space_id"],
                "expected_chunk_ids": list(expected_chunk_ids),
                "query_text": item["query_text"],
                "query_vector_checksum": checksum,
                "tenant_id": item["tenant_id"],
                "vector": vector,
            }
        )
    return tuple(queries)


def _expected_load_access(
    manifest: dict[str, Any],
) -> tuple[dict[str, set[str]], dict[str, Any], tuple[dict[str, Any], ...]]:
    """Derive the load principal's security contract from load-v1 source data."""

    coverage = manifest.get("coverage")
    counts = manifest.get("counts")
    if not isinstance(coverage, dict) or not isinstance(counts, dict):
        raise RuntimeError("load-v1 access coverage is invalid")
    groups = coverage.get("load_principal_groups")
    if (
        coverage.get("primary_load_tenant") != PRIMARY_TENANT_ID
        or not isinstance(groups, list)
        or len(groups) != 2
        or len(set(groups)) != len(groups)
        or any(not isinstance(group, str) or not group for group in groups)
    ):
        raise RuntimeError("load-v1 access principal is invalid")

    all_ids: set[str] = set()
    active_ids: set[str] = set()
    authorized_ids: set[str] = set()
    chunks_by_id: dict[str, dict[str, Any]] = {}
    principal_groups = set(groups)
    for chunk in iter_chunks():
        chunk_id = str(chunk["chunk_id"])
        if chunk_id in chunks_by_id:
            raise RuntimeError("load-v1 contains duplicate Chunk IDs")
        chunks_by_id[chunk_id] = chunk
        all_ids.add(chunk_id)
        if chunk["active"] is True:
            active_ids.add(chunk_id)
            if (
                chunk["tenant_id"] == PRIMARY_TENANT_ID
                and principal_groups.intersection(chunk["access_groups"])
            ):
                authorized_ids.add(chunk_id)
    inactive_ids = all_ids - active_ids
    forbidden_ids = active_ids - authorized_ids
    if (
        len(all_ids) != int(counts.get("total_chunks", -1))
        or len(active_ids) != int(counts.get("active_chunks", -1))
        or len(inactive_ids) != int(counts.get("historical_chunks", -1))
        or not authorized_ids
        or not forbidden_ids
    ):
        raise RuntimeError("load-v1 access inventory disagrees with its manifest")

    canary_groups = (
        ("same-tenant-denied", coverage.get("protected_same_tenant_chunk_ids")),
        ("cross-tenant-denied", coverage.get("cross_tenant_chunk_ids")),
    )
    probes: list[dict[str, Any]] = []
    canary_ids: set[str] = set()
    for probe_kind, raw_ids in canary_groups:
        if (
            not isinstance(raw_ids, list)
            or len(raw_ids) != 4
            or len(set(raw_ids)) != len(raw_ids)
            or any(not isinstance(item, str) or not item for item in raw_ids)
        ):
            raise RuntimeError(f"load-v1 {probe_kind} canaries are invalid")
        for index, chunk_id in enumerate(raw_ids):
            chunk = chunks_by_id.get(chunk_id)
            if (
                chunk is None
                or chunk["active"] is not True
                or chunk_id not in forbidden_ids
                or (
                    probe_kind == "same-tenant-denied"
                    and chunk["tenant_id"] != PRIMARY_TENANT_ID
                )
                or (
                    probe_kind == "cross-tenant-denied"
                    and chunk["tenant_id"] == PRIMARY_TENANT_ID
                )
            ):
                raise RuntimeError(f"load-v1 access canary drifted: {chunk_id}")
            case_id = f"{probe_kind}-{index:02d}"
            query_text = f"Stage 9 access-isolation probe {case_id}"
            vector = deterministic_vector(chunk_id)
            probes.append(
                {
                    "access_groups": tuple(str(item) for item in chunk["access_groups"]),
                    "canary_chunk_id": chunk_id,
                    "case_id": case_id,
                    "chunk_key": str(chunk["chunk_key"]),
                    "dataset_id": _LOAD_DATASET_ID,
                    "document_id": str(chunk["document_id"]),
                    "embedding_space_id": EMBEDDING_SPACE_ID,
                    "kind": probe_kind,
                    "query_text": query_text,
                    "query_text_sha256": _text_sha256(query_text),
                    "query_vector_checksum": f"sha256:{_vector_checksum(vector)}",
                    "source_text_sha256": _text_sha256(str(chunk["text"])),
                    "target_tenant_id": str(chunk["tenant_id"]),
                    "tenant_id": PRIMARY_TENANT_ID,
                    "vector": vector,
                    "version_id": str(chunk["version_id"]),
                }
            )
            canary_ids.add(chunk_id)
    if len(probes) != 8 or len(canary_ids) != 8:
        raise RuntimeError("load-v1 access canary coverage is incomplete")

    sets = {
        "active": active_ids,
        "all": all_ids,
        "authorized": authorized_ids,
        "forbidden": forbidden_ids,
        "inactive": inactive_ids,
    }
    inventory = {
        name: {
            "count": len(values),
            "chunk_ids_sha256": _canonical_digest(sorted(values)),
        }
        for name, values in sorted(sets.items())
    }
    contract = {
        "dataset_id": _LOAD_DATASET_ID,
        "inventory": inventory,
        "principal": {
            "groups": sorted(groups),
            "tenant_id": PRIMARY_TENANT_ID,
        },
        "schema_version": _ACCESS_EVIDENCE_SCHEMA_VERSION,
    }
    return sets, contract, tuple(probes)


def _answer_queries(config: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    configured = config.get("answer", {}).get("gold_case_ids")
    if (
        not isinstance(configured, list)
        or len(configured) != 30
        or len(set(configured)) != len(configured)
        or any(not isinstance(item, str) or not item for item in configured)
    ):
        raise RuntimeError("answer.gold_case_ids must contain 30 unique case IDs")
    if int(config["answer"]["latency_samples"]) != len(configured):
        raise RuntimeError("answer latency sample count must match configured gold cases")
    _answer_warmup_requests(config)
    questions_by_id = {item["id"]: item for item in _load_jsonl(_GOLD_QUESTIONS)}
    vectors_by_id = {
        str(item["id"]): tuple(float(value) for value in item["vector"])
        for item in _load_jsonl(_DEV_CORPUS_VECTORS)
        if item.get("kind") == "query"
    }
    manifest = json.loads(_DEV_CORPUS_MANIFEST.read_text(encoding="utf-8"))
    embedding_space_id = manifest["embedding_profile"]["embedding_space_id"]
    queries: list[dict[str, Any]] = []
    for case_id in configured:
        question = questions_by_id.get(case_id)
        if question is None or question.get("answerable") is not True:
            raise RuntimeError(f"configured answer case is not gold answered: {case_id}")
        expected_chunk_ids = sorted(str(item) for item in question["relevance"])
        if not expected_chunk_ids:
            raise RuntimeError(f"gold answer case has no expected Chunks: {case_id}")
        principal = question["principal"]
        vector = vectors_by_id.get(str(question.get("vector_id")))
        if vector is None:
            raise RuntimeError(f"configured answer case has no query vector: {case_id}")
        queries.append(
            {
                "case_id": case_id,
                "dataset_id": _GOLD_DATASET_ID,
                "embedding_space_id": embedding_space_id,
                "expected_chunk_ids": expected_chunk_ids,
                "groups": list(principal["groups"]),
                "principal_id": str(principal["principal_id"]),
                "query_text": str(question["query"]),
                "query_vector_checksum": _vector_checksum(vector),
                "tenant_id": str(principal["tenant_id"]),
                "vector": vector,
            }
        )
    return tuple(queries)


def _answer_warmup_requests(config: dict[str, Any]) -> int:
    value = config.get("answer", {}).get("warmup_requests")
    case_ids = config.get("answer", {}).get("gold_case_ids")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not isinstance(case_ids, list)
        or len(case_ids) != 30
        or value != len(case_ids)
    ):
        raise RuntimeError(
            "answer.warmup_requests must preflight all 30 configured answer cases"
        )
    return value


def _database_chunk_security_sets(
    driver: neo4j.Driver,
    database: str,
    tenant_id: str,
    groups: Iterable[str],
) -> tuple[set[str], set[str], set[str]]:
    """Derive all, authorized-active, and inactive IDs from the live graph."""

    all_records, _, _ = driver.execute_query(
        "MATCH (chunk:Chunk) RETURN DISTINCT chunk.chunk_id AS chunk_id",
        database_=database,
    )
    active_records, _, _ = driver.execute_query(
        """
        MATCH (document:Document)-[:ACTIVE_SNAPSHOT]->(
            snapshot:KnowledgeSnapshot {build_state: 'PUBLISHED'}
        )-[:INCLUDES_CHUNK]->(chunk:Chunk)
        MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion)
        MATCH (snapshot)-[:OF_VERSION]->(version)
        WHERE chunk.tenant_id = document.tenant_id
          AND version.tenant_id = document.tenant_id
          AND snapshot.tenant_id = document.tenant_id
          AND chunk.document_id = document.document_id
          AND chunk.version_id = version.version_id
        RETURN DISTINCT chunk.chunk_id AS chunk_id
        """,
        database_=database,
    )
    allowed_records, _, _ = driver.execute_query(
        """
        MATCH (document:Document {tenant_id: $tenant_id})
              -[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                  tenant_id: $tenant_id, build_state: 'PUBLISHED'
              })-[:INCLUDES_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
        MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
            tenant_id: $tenant_id
        })
        MATCH (snapshot)-[:OF_VERSION]->(version)
        WHERE any(group IN document.access_groups WHERE group IN $groups)
          AND any(group IN chunk.access_groups WHERE group IN $groups)
          AND chunk.document_id = document.document_id
          AND chunk.version_id = version.version_id
          AND chunk.access_policy_id = document.access_policy_id
          AND chunk.access_policy_version = document.access_policy_version
        RETURN DISTINCT chunk.chunk_id AS chunk_id
        """,
        tenant_id=tenant_id,
        groups=sorted(groups),
        database_=database,
    )
    all_ids = {str(record["chunk_id"]) for record in all_records}
    active_ids = {str(record["chunk_id"]) for record in active_records}
    allowed_ids = {str(record["chunk_id"]) for record in allowed_records}
    if (
        not all_ids
        or not allowed_ids
        or not allowed_ids <= active_ids
        or not active_ids <= all_ids
    ):
        raise RuntimeError("live database security inventory is inconsistent")
    return all_ids, allowed_ids, all_ids - active_ids


def _validate_database_access(
    live_all: set[str],
    live_allowed: set[str],
    live_inactive: set[str],
    expected: dict[str, set[str]],
) -> None:
    """Cross-check live graph authorization against the source-side contract."""

    expected_all = expected["all"]
    if expected_all - live_all:
        raise RuntimeError("load-v1 Chunk inventory is incomplete in the database")
    if live_inactive.intersection(expected_all) != expected["inactive"]:
        raise RuntimeError("load-v1 active/inactive inventory drifted in the database")
    if live_allowed != expected["authorized"]:
        raise RuntimeError("load-v1 live authorization drifted from committed Chunk ACLs")
    live_active_load = expected_all.intersection(live_all - live_inactive)
    if live_active_load - live_allowed != expected["forbidden"]:
        raise RuntimeError("load-v1 forbidden inventory drifted in the database")


def _start_server(
    config: dict[str, Any],
    port: int,
    query_values: tuple[dict[str, Any], ...],
    jwt_secret: bytes,
    predictions_by_query: dict[str, dict[str, Any]],
) -> _ReferenceServer:
    uri, user, password, database = _load_settings()
    driver = neo4j.GraphDatabase.driver(
        uri,
        auth=(user, password),
        max_connection_pool_size=config["neo4j"]["max_connection_pool_size"],
        connection_acquisition_timeout=config["neo4j"][
            "online_transaction_timeout_seconds"
        ],
    )
    driver.verify_connectivity()
    embedder = _ReferenceQueryEmbedder(
        {
            (item["tenant_id"], item["query_text"]): (
                item["vector"],
                item["embedding_space_id"],
            )
            for item in query_values
        },
        delay_ms=config["dependencies"]["embedding_provider"]["success_delay_ms"],
        timeout_delay_ms=config["dependencies"]["embedding_provider"][
            "timeout_delay_ms"
        ],
    )
    model = _ReferenceAnswerModel(
        delay_ms=config["dependencies"]["llm_provider"]["success_delay_ms"],
        timeout_delay_ms=config["dependencies"]["llm_provider"][
            "timeout_delay_ms"
        ],
        predictions_by_query=predictions_by_query,
    )
    generation = _MeteredReferenceGeneration(
        model,
        input_tokens=config["answer"]["provider_input_tokens"],
        output_tokens=config["answer"]["provider_output_tokens"],
        request_cost_usd=float(config["answer"]["provider_request_cost_usd"]),
    )
    normal_retrieval = Neo4jRetrievalEngine(
        driver,
        database,
        transaction_timeout_seconds=config["neo4j"][
            "online_transaction_timeout_seconds"
        ],
    )
    readiness = _Readiness(
        driver,
        database,
        transaction_timeout_seconds=config["neo4j"][
            "online_transaction_timeout_seconds"
        ],
    )
    retrieval = _SwitchableRetrievalEngine(normal_retrieval)
    queries = GraphRAGQueryOperations(
        retrieval,  # type: ignore[arg-type]
        embedder,
        generation,  # type: ignore[arg-type]
    )
    application_backend = GraphRAGApplicationBackend(
        documents=_UnavailableDocuments(),
        queries=queries,
        readiness=readiness,
    )
    retrieval_stages = _RetrievalStageRecorder(application_backend)
    api_config = config["api"]
    app = create_app(
        authenticator=JWTAuthenticator(
            JWTAuthConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                secret=jwt_secret,
                leeway_seconds=0,
            )
        ),
        backend=retrieval_stages,
        settings=APISettings(
            service_name="sample-graphrag-stage9-reference",
            version=config["version"],
        ),
        runtime_policy=RuntimePolicy(
            max_workers=api_config["max_workers"],
            max_queue_size=api_config["max_queue_size"],
            timeout_seconds=api_config["timeout_seconds"],
            max_attempts=api_config["max_attempts"],
        ),
        rate_limit_policy=RateLimitPolicy(
            requests=api_config["rate_limit_requests"],
            window_seconds=api_config["rate_limit_window_seconds"],
            burst_capacity=api_config["rate_limit_burst"],
        ),
        logger=_NullLogger(),  # type: ignore[arg-type]
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="stage9-uvicorn", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    live = False
    while time.monotonic() < deadline:
        try:
            if not live:
                response = httpx.get(f"{base_url}/health/live", timeout=1)
                live = response.status_code == 200
        except httpx.HTTPError:
            pass
        if live and _probe_reference_readiness(base_url):
            break
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        driver.close()
        raise RuntimeError("Stage 9 reference API did not become ready")
    return _ReferenceServer(
        server,
        thread,
        embedder,
        generation,
        driver,
        retrieval,
        retrieval_stages,
        normal_retrieval.transaction_timeout_seconds,
        readiness.transaction_timeout_seconds,
        "ready",
    )


def _retrieval_body(config: dict[str, Any], query_text: str) -> dict[str, Any]:
    return {
        "limits": config["retrieval"]["limits"],
        "query_text": query_text,
        "version_filter": {},
    }


def _answer_body(config: dict[str, Any], query_text: str) -> dict[str, Any]:
    try:
        answer_limits = resolve_production_answer_retrieval_limits(config)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return {
        "generation_limits": {},
        "query_text": query_text,
        "retrieval_limits": asdict(answer_limits),
        "version_filter": {},
    }


def _error_code(response: httpx.Response) -> str | None:
    if response.status_code < 400:
        return None
    try:
        value = response.json().get("code")
    except (json.JSONDecodeError, AttributeError):
        return "invalid_error_response"
    return value if isinstance(value, str) else "invalid_error_response"


def _trace_chunk_ids(payload: dict[str, Any]) -> set[str]:
    values = _trace_only_chunk_ids(payload)
    for item in payload.get("chunks", []):
        citation = item.get("citation", {})
        chunk_id = citation.get("chunk_id")
        if isinstance(chunk_id, str):
            values.add(chunk_id)
    for citation in payload.get("citations", []):
        chunk_id = citation.get("chunk_id")
        if isinstance(chunk_id, str):
            values.add(chunk_id)
    return values


def _trace_only_chunk_ids(payload: dict[str, Any]) -> set[str]:
    trace = payload.get("trace", {})
    if not isinstance(trace, dict):
        return set()
    values: set[str] = {
        str(item)
        for item in trace.get("selected_chunk_ids", [])
        if isinstance(item, str)
    }
    for stage in (
        "vector_recall",
        "bm25_recall",
        "seed_ranking",
        "graph_expansion",
        "candidate_vector_ranking",
        "final_ranking",
    ):
        for item in trace.get(stage, []):
            chunk_id = item.get("chunk_id")
            if isinstance(chunk_id, str):
                values.add(chunk_id)
    return values


def _selected_chunk_ids(payload: dict[str, Any], kind: str) -> set[str]:
    if kind == "retrieval":
        return set(payload.get("trace", {}).get("selected_chunk_ids", []))
    return {
        str(item["chunk_id"])
        for item in payload.get("citations", [])
        if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
    }


def _result_chunk_ids(payload: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in payload.get("chunks", []):
        if not isinstance(item, dict):
            continue
        citation = item.get("citation", {})
        if isinstance(citation, dict) and isinstance(citation.get("chunk_id"), str):
            result.add(str(citation["chunk_id"]))
    return result


def _citation_chunk_ids(payload: dict[str, Any]) -> set[str]:
    return {
        str(item["chunk_id"])
        for item in payload.get("citations", [])
        if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
    }


def _payload_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _payload_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _payload_strings(item)


def _payload_exposes_canary(payload: dict[str, Any], probe: dict[str, Any]) -> bool:
    strings = tuple(_payload_strings(payload))
    markers = {
        str(probe["canary_chunk_id"]),
        str(probe["chunk_key"]),
        str(probe["document_id"]),
        *probe["access_groups"],
    }
    if probe["target_tenant_id"] != PRIMARY_TENANT_ID:
        markers.add(str(probe["target_tenant_id"]))
    if any(marker in value for marker in markers for value in strings):
        return True
    return any(_text_sha256(value) == probe["source_text_sha256"] for value in strings)


def _access_probe_body(
    config: dict[str, Any], query_text: str, version_id: str
) -> dict[str, Any]:
    body = _retrieval_body(config, query_text)
    body["limits"] = {
        **body["limits"],
        "minimum_bm25_score": 1_000_000.0,
        "minimum_vector_score": 0.999999,
    }
    body["version_filter"] = {"version_ids": [version_id]}
    return body


def _access_isolation_fault(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    config: dict[str, Any],
    contract: dict[str, Any],
    probes: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Issue real denied retrievals and retain their public HTTP evidence."""

    event_started = time.monotonic_ns()
    evidence_probes: list[dict[str, Any]] = []
    probe_passes: list[bool] = []
    for probe in probes:
        started = time.monotonic_ns()
        request_id = f"access-{probe['case_id']}-{started}"
        response = client.post(
            "/v1/retrieval",
            headers={**headers, "X-Request-ID": request_id},
            json=_access_probe_body(
                config,
                str(probe["query_text"]),
                str(probe["version_id"]),
            ),
        )
        completed = time.monotonic_ns()
        payload = response.json()
        trace_ids = _trace_only_chunk_ids(payload)
        result_ids = _result_chunk_ids(payload)
        citation_ids = _citation_chunk_ids(payload)
        response_request_id = response.headers.get("x-request-id")
        trace_id = response.headers.get("x-trace-id")
        error_code = _error_code(response)
        passed = (
            response.status_code == 200
            and error_code is None
            and response_request_id == request_id
            and isinstance(trace_id, str)
            and bool(trace_id)
            and not trace_ids
            and not result_ids
            and not citation_ids
            and not _payload_exposes_canary(payload, probe)
        )
        probe_passes.append(passed)
        evidence_probes.append(
            {
                "canary_chunk_id": probe["canary_chunk_id"],
                "case_id": probe["case_id"],
                "citation_chunk_ids": sorted(citation_ids),
                "completed_monotonic_ms": completed / 1_000_000,
                "embedding_space_id": probe["embedding_space_id"],
                "error_code": error_code,
                "http_status": response.status_code,
                "kind": probe["kind"],
                "query_text_sha256": probe["query_text_sha256"],
                "query_vector_checksum": probe["query_vector_checksum"],
                "request_id": request_id,
                "response": payload,
                "result_chunk_ids": sorted(result_ids),
                "started_monotonic_ms": started / 1_000_000,
                "target_tenant_id": probe["target_tenant_id"],
                "trace_chunk_ids": sorted(trace_ids),
                "trace_id": trace_id,
                "version_id": probe["version_id"],
            }
        )
    event_finished = time.monotonic_ns()
    return {
        "access_evidence": {**contract, "probes": evidence_probes},
        "domain_failure_code": None,
        "domain_status": None,
        "error_code": None,
        "finished_ns": event_finished,
        "http_status": 200,
        "latency_ms": (event_finished - event_started) / 1_000_000,
        "passed": all(probe_passes),
        "reason": None,
        "scenario_id": "access_isolation",
        "started_ns": event_started,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def _attach_retrieval_stage_samples(
    rows: list[dict[str, Any]],
    recorder: _RetrievalStageRecorder,
) -> list[dict[str, Any]]:
    """Bind backend retrieval-stage timing to each measured HTTP request."""

    request_ids = {str(row["request_id"]) for row in rows}
    if len(request_ids) != len(rows):
        raise RuntimeError("measured request identities are not unique")
    samples = recorder.samples_for(request_ids)
    indexed = {str(sample["request_id"]): sample for sample in samples}
    for row in rows:
        sample = indexed[str(row["request_id"])]
        if sample["trace_id"] != row["trace_id"]:
            raise RuntimeError("retrieval-stage trace identity does not match response")
        stage_ms = float(sample["retrieval_stage_ms"])
        request_ms = float(row["completed_monotonic_ms"]) - float(
            row["started_monotonic_ms"]
        )
        if not 0.0 < stage_ms <= request_ms + 1e-6:
            raise RuntimeError("retrieval-stage timing is outside its HTTP request")
        row["retrieval_stage_ms"] = stage_ms
    return samples


def _request_row(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    path: str,
    body: dict[str, Any],
    client_id: str,
    kind: str,
    dataset_id: str,
    case_id: str,
    allowed: set[str],
    historical: set[str],
    expected_chunk_ids: Iterable[str],
    embedding_space_id: str,
    query_vector_checksum: str,
) -> dict[str, Any]:
    expected_ids = frozenset(expected_chunk_ids)
    if not expected_ids:
        raise ValueError("measured requests require expected Chunk IDs")
    started = time.monotonic_ns()
    request_id = f"{client_id}-{started}"
    request_headers = {**headers, "X-Request-ID": request_id}
    try:
        response = client.post(path, headers=request_headers, json=body)
        status_code = response.status_code
        payload = response.json()
        if response.headers.get("x-request-id") != request_id:
            raise RuntimeError("API response changed the measured request identity")
        observed_ids = _trace_chunk_ids(payload) if status_code == 200 else set()
        observed_forbidden = observed_ids - allowed
        observed_historical = observed_ids & historical
        selected_ids = _selected_chunk_ids(payload, kind)
        trace_id = response.headers.get("x-trace-id")
        domain_status = payload.get("status") if kind == "answer" else None
        domain_failure_code = (
            payload.get("failure_code") if kind == "answer" else None
        )
        answer_evidence = (
            build_http_answer_commitment(payload)
            if kind == "answer" and status_code == 200
            else None
        )
    except (httpx.HTTPError, json.JSONDecodeError) as error:
        status_code = 599
        payload = {}
        observed_forbidden = set()
        trace_id = None
        domain_status = None
        domain_failure_code = type(error).__name__
        observed_historical = set()
        selected_ids = set()
        answer_evidence = None
    finished = time.monotonic_ns()
    semantic_success = (
        status_code == 200
        and not expected_ids.isdisjoint(selected_ids)
        and not observed_forbidden
        and not observed_historical
        and (
            kind != "answer"
            or (domain_status == "answered" and domain_failure_code is None)
        )
    )
    return {
        "answer_evidence": answer_evidence,
        "client_id": client_id,
        "completed_monotonic_ms": finished / 1_000_000,
        "case_id": case_id,
        "dataset_id": dataset_id,
        "domain_failure_code": domain_failure_code,
        "domain_status": domain_status,
        "error_code": (
            _error_code(response) if "response" in locals() else "transport_error"
        ),
        "embedding_space_id": embedding_space_id,
        "expected_chunk_ids": sorted(expected_ids),
        "inactive_chunk_ids": sorted(observed_historical),
        "inactive_version_count": len(observed_historical),
        "kind": kind,
        "request_id": request_id,
        "query_vector_checksum": query_vector_checksum,
        "selected_chunk_ids": sorted(selected_ids),
        "selected_chunk_count": len(selected_ids),
        "semantic_success": semantic_success,
        "started_monotonic_ms": started / 1_000_000,
        "status_code": status_code,
        "trace_id": trace_id,
        "unauthorized_chunk_ids": sorted(observed_forbidden),
        "unauthorized_chunk_count": len(observed_forbidden),
        "visible_chunk_ids": sorted(observed_ids),
    }


def _require_reference_answer(row: dict[str, Any], *, phase: str) -> None:
    """Fail with the exact case and retrieval evidence needed for diagnosis."""

    if (
        row.get("status_code") == 200
        and row.get("domain_status") == "answered"
        and row.get("domain_failure_code") is None
        and row.get("error_code") is None
        and row.get("semantic_success") is True
        and row.get("unauthorized_chunk_count") == 0
        and row.get("inactive_version_count") == 0
    ):
        return
    raise RuntimeError(
        f"Stage 9 {phase} did not pass grounding gates: "
        f"case_id={row.get('case_id')}; "
        f"status_code={row.get('status_code')}; "
        f"domain_status={row.get('domain_status')}; "
        f"domain_failure_code={row.get('domain_failure_code')}; "
        f"error_code={row.get('error_code')}; "
        f"expected={row.get('expected_chunk_ids')}; "
        f"selected={row.get('selected_chunk_ids')}; "
        f"unauthorized={row.get('unauthorized_chunk_ids')}; "
        f"inactive={row.get('inactive_chunk_ids')}"
    )


def _require_answer_preflight_coverage(
    observed_case_ids: Iterable[str],
    answer_queries: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    """Prove that every configured answer case ran exactly once."""

    observed = tuple(observed_case_ids)
    expected = tuple(str(query["case_id"]) for query in answer_queries)
    if len(observed) != len(expected) or sorted(observed) != sorted(expected):
        raise RuntimeError(
            "answer preflight did not cover every configured case exactly once"
        )
    return tuple(sorted(observed))


def _fault_request(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    path: str,
    body: dict[str, Any],
    scenario_id: str,
    expected_status: int,
    expected_code: str | None,
    expected_domain_failure: str | None = None,
    expected_domain_status: str | None = None,
    expected_latency_range_ms: tuple[float, float] | None = None,
) -> dict[str, Any]:
    started = time.monotonic_ns()
    response = client.post(path, headers=headers, json=body)
    finished = time.monotonic_ns()
    payload = response.json()
    public_code = _error_code(response)
    domain_failure = payload.get("failure_code") if response.status_code == 200 else None
    domain_status = payload.get("status")
    if scenario_id == "neo4j_success" and response.status_code == 200:
        domain_status = "retrieved" if payload.get("chunks") else "empty"
    if domain_failure is not None:
        # A provider response rejected by the grounded-output boundary is a
        # successful, safe refusal at the API boundary, not a transport 5xx.
        domain_status = "refused"
    passed = response.status_code == expected_status and public_code == expected_code
    if expected_domain_failure is not None:
        passed = (
            passed
            and domain_failure == expected_domain_failure
            and not payload.get("citations", [])
        )
    if expected_domain_status is not None:
        passed = passed and domain_status == expected_domain_status
    latency_ms = (finished - started) / 1_000_000
    if expected_latency_range_ms is not None:
        lower_ms, upper_ms = expected_latency_range_ms
        passed = passed and lower_ms <= latency_ms <= upper_ms
    return {
        "domain_failure_code": domain_failure,
        "domain_status": domain_status,
        "error_code": public_code,
        "finished_ns": finished,
        "http_status": response.status_code,
        "latency_ms": latency_ms,
        "passed": bool(passed),
        "reason": domain_failure,
        "scenario_id": scenario_id,
        "started_ns": started,
    }


def _neo4j_fault_engines(
    *,
    server_driver: neo4j.Driver,
    unavailable_driver: neo4j.Driver,
    database: str,
    online_timeout_seconds: float,
) -> tuple[
    tuple[str, Neo4jRetrievalEngine, int, str | None, str | None],
    ...,
]:
    normal = Neo4jRetrievalEngine(
        server_driver,
        database,
        transaction_timeout_seconds=online_timeout_seconds,
    )
    return (
        ("success", normal, 200, None, "retrieved"),
        (
            "timeout",
            _SlowNeo4jRetrievalEngine(
                server_driver,
                database,
                transaction_timeout_seconds=0.001,
            ),
            504,
            "dependency_timeout",
            None,
        ),
        (
            "unavailable",
            Neo4jRetrievalEngine(
                unavailable_driver,
                database,
                transaction_timeout_seconds=online_timeout_seconds,
            ),
            503,
            "dependency_unavailable",
            None,
        ),
        (
            "failure",
            _InvalidNeo4jRetrievalEngine(
                server_driver,
                database,
                transaction_timeout_seconds=online_timeout_seconds,
            ),
            500,
            "internal_error",
            None,
        ),
    )


def _neo4j_faults(
    *,
    server: _ReferenceServer,
    client: httpx.Client,
    headers: dict[str, str],
    config: dict[str, Any],
    query: dict[str, Any],
) -> list[dict[str, Any]]:
    _uri, user, password, database = _load_settings()
    unavailable_driver = neo4j.GraphDatabase.driver(
        "bolt://127.0.0.1:1",
        auth=(user, password),
        connection_timeout=0.2,
        connection_acquisition_timeout=0.2,
        max_transaction_retry_time=0,
    )
    engines = _neo4j_fault_engines(
        server_driver=server.driver,
        unavailable_driver=unavailable_driver,
        database=database,
        online_timeout_seconds=float(
            config["neo4j"]["online_transaction_timeout_seconds"]
        ),
    )
    normal = engines[0][1]
    rows: list[dict[str, Any]] = []
    try:
        for mode, engine, status, code, domain_status in engines:
            server.retrieval.set_delegate(engine)
            rows.append(
                _fault_request(
                    client=client,
                    headers=headers,
                    path="/v1/retrieval",
                    body=_retrieval_body(config, query["query_text"]),
                    scenario_id=f"neo4j_{mode}",
                    expected_status=status,
                    expected_code=code,
                    expected_domain_status=domain_status,
                )
            )
    finally:
        server.retrieval.set_delegate(normal)
        unavailable_driver.close()
    return rows


def run(config_path: Path, output_dir: Path, port: int) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider_timeout_windows = {
        provider_id: _provider_timeout_window_ms(config, provider_id)
        for provider_id in _PROVIDER_TIMEOUT_IDS
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    queries = _queries()
    answer_queries = _answer_queries(config)
    answer_warmup_requests = _answer_warmup_requests(config)
    predictions_by_query = load_reference_predictions(_REFERENCE_PREDICTIONS)
    for query in answer_queries:
        prediction = predictions_by_query.get(query["query_text"])
        if (
            prediction is None
            or prediction["id"] != query["case_id"]
            or prediction["status"] != "answered"
        ):
            raise RuntimeError("recorded HTTP answer predictions drifted from gold cases")
    manifest = build_manifest()
    workload = manifest["retrieval_workload"]
    if (
        config["retrieval"]["limits"]["minimum_vector_score"]
        != workload["minimum_anchor_cosine"]
    ):
        raise RuntimeError(
            "retrieval minimum_vector_score does not match the load anchor contract"
        )
    if config["retrieval"]["warmup_requests"] != workload["query_count"]:
        raise RuntimeError(
            "retrieval warmup must preflight every versioned load query exactly once"
        )
    access_sets, access_contract, access_probes = _expected_load_access(manifest)
    groups = manifest["coverage"]["load_principal_groups"]
    actual_port = _available_port(port)
    secret = secrets.token_bytes(32)
    server = _start_server(
        config,
        actual_port,
        queries + answer_queries + access_probes,
        secret,
        predictions_by_query,
    )
    _, _, _, database = _load_settings()
    all_database_chunks, live_allowed, live_historical = _database_chunk_security_sets(
        server.driver,
        database,
        PRIMARY_TENANT_ID,
        groups,
    )
    _validate_database_access(
        all_database_chunks,
        live_allowed,
        live_historical,
        access_sets,
    )
    allowed = access_sets["authorized"]
    historical = access_sets["inactive"]
    forbidden = access_sets["forbidden"]
    answer_access: dict[str, tuple[set[str], set[str]]] = {}
    for query in answer_queries:
        case_all, case_allowed, case_historical = _database_chunk_security_sets(
            server.driver,
            database,
            query["tenant_id"],
            query["groups"],
        )
        if case_all != all_database_chunks or case_historical != live_historical:
            raise RuntimeError("gold answer security inventory is inconsistent")
        if set(query["expected_chunk_ids"]).isdisjoint(case_allowed):
            raise RuntimeError(
                f"gold answer expected Chunks are inaccessible: {query['case_id']}"
            )
        answer_access[query["case_id"]] = (case_allowed, case_historical)
    base_url = f"http://127.0.0.1:{actual_port}"
    tokens = [
        _token(
            secret,
            f"stage9-load-client-{index:02d}",
            groups,
            tenant_id=PRIMARY_TENANT_ID,
        )
        for index in range(config["retrieval"]["concurrency"])
    ]
    headers = [{"Authorization": f"Bearer {token}"} for token in tokens]
    answer_headers = {
        query["case_id"]: {
            "Authorization": "Bearer "
            + _token(
                secret,
                query["principal_id"],
                query["groups"],
                tenant_id=query["tenant_id"],
            )
        }
        for query in answer_queries
    }
    rows: list[dict[str, Any]] = []
    fault_rows: list[dict[str, Any]] = []
    answer_preflight_case_ids: list[str] = []
    try:
        with httpx.Client(base_url=base_url, timeout=10) as warm_client:
            for index in range(config["retrieval"]["warmup_requests"]):
                query = queries[index % len(queries)]
                response = warm_client.post(
                    "/v1/retrieval",
                    headers=headers[index % len(headers)],
                    json=_retrieval_body(
                        config,
                        query["query_text"],
                    ),
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Stage 9 warmup failed with HTTP {response.status_code}"
                    )
                warm_payload = response.json()
                if not _trace_chunk_ids(warm_payload) <= allowed:
                    raise RuntimeError("warmup exposed unauthorized Chunk IDs")
                expected = set(query["expected_chunk_ids"])
                selected = _selected_chunk_ids(warm_payload, "retrieval")
                if expected.isdisjoint(selected):
                    raise RuntimeError(
                        "warmup did not retrieve its expected anchor: "
                        f"case_id={query['case_id']}; "
                        f"expected={sorted(expected)}; selected={sorted(selected)}"
                    )

            warmup_answer_calls_before = server.generation.metered_calls
            for index in range(answer_warmup_requests):
                query = answer_queries[index % len(answer_queries)]
                answer_allowed, answer_historical = answer_access[query["case_id"]]
                warmup_row = _request_row(
                    client=warm_client,
                    headers=answer_headers[query["case_id"]],
                    path="/v1/answers",
                    body=_answer_body(config, query["query_text"]),
                    client_id=f"answer-warmup-{index:02d}",
                    kind="answer",
                    dataset_id=query["dataset_id"],
                    case_id=query["case_id"],
                    allowed=answer_allowed,
                    historical=answer_historical,
                    expected_chunk_ids=query["expected_chunk_ids"],
                    embedding_space_id=query["embedding_space_id"],
                    query_vector_checksum=query["query_vector_checksum"],
                )
                _require_reference_answer(warmup_row, phase="answer preflight")
                answer_preflight_case_ids.append(query["case_id"])
            answer_preflight_case_ids = list(
                _require_answer_preflight_coverage(
                    answer_preflight_case_ids,
                    answer_queries,
                )
            )
            answer_warmup_model_calls = (
                server.generation.metered_calls - warmup_answer_calls_before
            )
            if answer_warmup_model_calls != answer_warmup_requests:
                raise RuntimeError("answer warmup did not make exactly one model call each")

        embedding_success_before = server.embedder.successful_calls
        embedding_latency_before = len(server.embedder.latencies_ms)
        answer_success_before = server.generation.metered_calls
        llm_latency_before = len(server.generation.model.latencies_ms)
        start_barrier = threading.Barrier(config["retrieval"]["concurrency"])
        required_duration_ms = (
            float(config["retrieval"]["sustained_seconds"]) * 1_000
        )

        def worker(worker_index: int) -> list[dict[str, Any]]:
            local: list[dict[str, Any]] = []
            request_number = 0
            required_completion_ms: float | None = None
            with httpx.Client(base_url=base_url, timeout=10) as client:
                start_barrier.wait(timeout=30)
                while (
                    required_completion_ms is None
                    or local[-1]["completed_monotonic_ms"] < required_completion_ms
                ):
                    query = queries[(worker_index + request_number) % len(queries)]
                    row = _request_row(
                        client=client,
                        headers=headers[worker_index],
                        path="/v1/retrieval",
                        body=_retrieval_body(config, query["query_text"]),
                        client_id=f"retrieval-{worker_index:02d}",
                        kind="retrieval",
                        dataset_id=query["dataset_id"],
                        case_id=query["case_id"],
                        allowed=allowed,
                        historical=historical,
                        expected_chunk_ids=query["expected_chunk_ids"],
                        embedding_space_id=query["embedding_space_id"],
                        query_vector_checksum=query["query_vector_checksum"],
                    )
                    local.append(row)
                    if required_completion_ms is None:
                        required_completion_ms = (
                            float(row["started_monotonic_ms"])
                            + required_duration_ms
                        )
                    request_number += 1
            return local

        with ThreadPoolExecutor(
            max_workers=config["retrieval"]["concurrency"],
            thread_name_prefix="stage9-load",
        ) as pool:
            for local_rows in pool.map(
                worker, range(config["retrieval"]["concurrency"])
            ):
                rows.extend(local_rows)

        with httpx.Client(base_url=base_url, timeout=20) as answer_client:
            for index, query in enumerate(answer_queries):
                answer_allowed, answer_historical = answer_access[query["case_id"]]
                row = _request_row(
                    client=answer_client,
                    headers=answer_headers[query["case_id"]],
                    path="/v1/answers",
                    body=_answer_body(config, query["query_text"]),
                    client_id=f"answer-{index:02d}",
                    kind="answer",
                    dataset_id=query["dataset_id"],
                    case_id=query["case_id"],
                    allowed=answer_allowed,
                    historical=answer_historical,
                    expected_chunk_ids=query["expected_chunk_ids"],
                    embedding_space_id=query["embedding_space_id"],
                    query_vector_checksum=query["query_vector_checksum"],
                )
                _require_reference_answer(row, phase="measured answer")
                rows.append(row)

            measured_embedding_calls = (
                server.embedder.successful_calls - embedding_success_before
            )
            measured_answer_calls = (
                server.generation.metered_calls - answer_success_before
            )
            measured_embedding_latencies = server.embedder.latencies_ms[
                embedding_latency_before:
            ]
            measured_llm_latencies = server.generation.model.latencies_ms[
                llm_latency_before:
            ]
            retrieval_success_query = queries[0]["query_text"]
            for mode, expected_status, expected_code in (
                ("success", 200, None),
                ("timeout", 504, "dependency_timeout"),
                ("unavailable", 503, "dependency_unavailable"),
                ("failure", 503, "dependency_unavailable"),
            ):
                server.embedder.set_mode(mode)
                fault_rows.append(
                    _fault_request(
                        client=answer_client,
                        headers=headers[0],
                        path="/v1/retrieval",
                        body=_retrieval_body(config, retrieval_success_query),
                        scenario_id=f"embedding_provider_{mode}",
                        expected_status=expected_status,
                        expected_code=expected_code,
                        expected_latency_range_ms=(
                            provider_timeout_windows["embedding_provider"]
                            if mode == "timeout"
                            else None
                        ),
                    )
                )
                if mode == "timeout" and not server.embedder.wait_for_idle(
                    config["dependencies"]["embedding_provider"][
                        "timeout_delay_ms"
                    ]
                    / 1_000.0
                    + 1.0
                ):
                    raise RuntimeError(
                        "embedding timeout probe did not drain its provider call"
                    )
            server.embedder.set_mode("success")

            answer_fault_query = answer_queries[0]
            for mode, expected_status, expected_code, domain_failure in (
                ("success", 200, None, None),
                ("timeout", 504, "dependency_timeout", None),
                ("unavailable", 503, "dependency_unavailable", None),
                ("failure", 200, None, "invalid_model_output"),
            ):
                server.generation.set_mode(mode)
                fault_rows.append(
                    _fault_request(
                        client=answer_client,
                        headers=answer_headers[answer_fault_query["case_id"]],
                        path="/v1/answers",
                        body=_answer_body(config, answer_fault_query["query_text"]),
                        scenario_id=f"llm_{mode}",
                        expected_status=expected_status,
                        expected_code=expected_code,
                        expected_domain_failure=domain_failure,
                        expected_latency_range_ms=(
                            provider_timeout_windows["llm_provider"]
                            if mode == "timeout"
                            else None
                        ),
                    )
                )
                if mode == "timeout" and not server.generation.wait_for_idle(
                    config["dependencies"]["llm_provider"]["timeout_delay_ms"]
                    / 1_000.0
                    + 1.0
                ):
                    raise RuntimeError("LLM timeout probe did not drain its provider call")
            server.generation.set_mode("success")

        with httpx.Client(base_url=base_url, timeout=10) as neo4j_client:
            fault_rows.extend(
                _neo4j_faults(
                    server=server,
                    client=neo4j_client,
                    headers=headers[0],
                    config=config,
                    query=queries[0],
                )
            )
        with httpx.Client(base_url=base_url, timeout=10) as access_client:
            fault_rows.append(
                _access_isolation_fault(
                    client=access_client,
                    headers=headers[0],
                    config=config,
                    contract=access_contract,
                    probes=access_probes,
                )
            )
        unauthorized = sum(row["unauthorized_chunk_count"] for row in rows)
        semantic_failures = sum(not row["semantic_success"] for row in rows)
        if unauthorized:
            raise RuntimeError("Stage 9 HTTP trace exposed unauthorized Chunk IDs")
        if semantic_failures:
            raise RuntimeError(
                f"Stage 9 measured {semantic_failures} semantically invalid 2xx responses"
            )
        if any(not row["passed"] for row in fault_rows):
            failed = [row["scenario_id"] for row in fault_rows if not row["passed"]]
            raise RuntimeError(f"Stage 9 dependency fault checks failed: {failed}")

        retrieval_stage_rows = _attach_retrieval_stage_samples(
            rows,
            server.retrieval_stages,
        )
        rows.sort(
            key=lambda item: (
                item["started_monotonic_ms"], item["client_id"], item["request_id"]
            )
        )
        fault_rows.sort(key=lambda item: item["scenario_id"])
        _write_jsonl(output_dir / "requests.jsonl", rows)
        _write_jsonl(
            output_dir / "retrieval-stage.jsonl",
            retrieval_stage_rows,
        )
        _write_jsonl(output_dir / "runtime-faults.jsonl", fault_rows)
        _write_json(
            output_dir / "provider-usage.json",
            {
                "answer_preflight_case_ids": sorted(answer_preflight_case_ids),
                "answer_warmup_model_calls": answer_warmup_model_calls,
                "answer_request_cost_usd": [
                    float(config["answer"]["provider_request_cost_usd"])
                ] * measured_answer_calls,
                "diagnostic_total_answer_model_calls": server.generation.calls,
                "diagnostic_total_embedding_model_calls": server.embedder.calls,
                "embedding_latency_ms": measured_embedding_latencies,
                "input_tokens": (
                    measured_embedding_calls * 12
                    + measured_answer_calls
                    * int(config["answer"]["provider_input_tokens"])
                ),
                "llm_latency_ms": measured_llm_latencies,
                "measured_answer_model_calls": measured_answer_calls,
                "measured_embedding_model_calls": measured_embedding_calls,
                "mode": "deterministic_reference",
                "model_calls": measured_embedding_calls + measured_answer_calls,
                "output_tokens": (
                    measured_answer_calls
                    * int(config["answer"]["provider_output_tokens"])
                ),
                "peak_concurrency": server.embedder.peak_concurrency,
                "schema_version": "production-provider-usage-v1",
            },
        )
        _write_json(
            output_dir / "load-window.json",
            {
                "answer_samples": sum(row["kind"] == "answer" for row in rows),
                "answer_warmup_requests": answer_warmup_requests,
                "configured_sustained_seconds": config["retrieval"][
                    "sustained_seconds"
                ],
                "forbidden_chunk_count": len(forbidden),
                "http_port": actual_port,
                "primary_tenant_active_chunks": manifest["coverage"][
                    "primary_tenant"
                ]["active_chunks"],
                "readiness_probe_status": server.readiness_probe_status,
                "readiness_transaction_timeout_seconds": (
                    server.readiness_transaction_timeout_seconds
                ),
                "retrieval_samples": sum(
                    row["kind"] == "retrieval" for row in rows
                ),
                "retrieval_transaction_timeout_seconds": (
                    server.retrieval_transaction_timeout_seconds
                ),
                "semantic_failure_count": semantic_failures,
                "schema_version": "production-load-window-v2",
                "warmup_requests": config["retrieval"]["warmup_requests"],
            },
        )
    finally:
        server.server.should_exit = True
        server.thread.join(timeout=15)
        server.driver.close()
        if server.thread.is_alive():
            raise RuntimeError("Stage 9 reference API did not stop cleanly")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("evaluation/production-reference-config.v1.json"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.port < 0 or args.port > 65_535:
        parser.error("--port must be zero or a valid TCP port")
    run(args.config, args.output_dir, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
