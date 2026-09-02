"""Unit checks for Stage 9 per-request retrieval-stage evidence."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import httpx
import neo4j

from graphrag_prod.api.runtime import (
    Backend,
    BackendResult,
    BoundedOperationRunner,
    DependencyTimeoutError,
    OperationEnvelope,
    OperationKind,
    RuntimePolicy,
    UsageMetadata,
)
from graphrag_prod.generation import GenerationRequest
from graphrag_prod.evaluation.production_config import (
    PRODUCTION_ANSWER_RETRIEVAL_LIMITS,
    resolve_production_answer_retrieval_limits,
)
from scripts.build_production_report import (
    _container_inspection_projection,
    _load_window_timeouts,
    _provider_timeout_window_ms as _report_timeout_window_ms,
    _retrieval_stage_samples,
    _validate_provider_timeout_scenarios,
)
from scripts.run_production_load import (
    _MeteredReferenceGeneration,
    _ReferenceAnswerModel,
    _ReferenceQueryEmbedder,
    _Readiness,
    _RetrievalStageRecorder,
    _answer_body,
    _answer_warmup_requests,
    _attach_retrieval_stage_samples,
    _neo4j_fault_engines,
    _probe_reference_readiness,
    _provider_timeout_window_ms,
    _require_answer_preflight_coverage,
    _require_reference_answer,
)


class _Backend:
    def __init__(self, usage: UsageMetadata) -> None:
        self.usage = usage

    def execute(self, _envelope: OperationEnvelope, /) -> BackendResult:
        return BackendResult({"ok": True}, self.usage)


class _EmbeddingBackend:
    def __init__(self, embedder: _ReferenceQueryEmbedder) -> None:
        self.embedder = embedder

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        return BackendResult(
            self.embedder.embed("Question?", tenant_id=envelope.tenant_id)
        )


class _GenerationBackend:
    def __init__(self, generation: _MeteredReferenceGeneration) -> None:
        self.generation = generation

    def execute(self, _envelope: OperationEnvelope, /) -> BackendResult:
        return BackendResult(
            self.generation.generate_with_usage(GenerationRequest("Question?", ()))
        )


def _envelope(
    request_id: str = "request-1",
    *,
    operation: OperationKind = OperationKind.RETRIEVAL,
) -> OperationEnvelope:
    return OperationEnvelope(
        operation=operation,
        request_id=request_id,
        trace_id=f"trace-{request_id}",
        principal_id="principal-1",
        tenant_id="tenant-1",
    )


def _usage(*, retrieval_ms: float = 4.5, stage_ms: float = 4.5) -> UsageMetadata:
    return UsageMetadata(
        retrieval_ms=retrieval_ms,
        stages=(("query_embedding", 1.0), ("retrieval", stage_ms)),
    )


def _row(request_id: str = "request-1") -> dict[str, object]:
    return {
        "completed_monotonic_ms": 110.0,
        "request_id": request_id,
        "started_monotonic_ms": 100.0,
        "trace_id": f"trace-{request_id}",
    }


class RetrievalStageRecorderTests(unittest.TestCase):
    def test_records_and_binds_one_stage_to_the_same_http_request(self) -> None:
        recorder = _RetrievalStageRecorder(_Backend(_usage()))
        result = recorder.execute(_envelope())
        row = _row()

        samples = _attach_retrieval_stage_samples([row], recorder)

        self.assertEqual(result.usage.retrieval_ms, 4.5)
        self.assertEqual(row["retrieval_stage_ms"], 4.5)
        self.assertEqual(
            samples,
            [
                {
                    "request_id": "request-1",
                    "retrieval_stage_ms": 4.5,
                    "trace_id": "trace-request-1",
                }
            ],
        )

    def test_rejects_inconsistent_backend_stage_metadata(self) -> None:
        recorder = _RetrievalStageRecorder(
            _Backend(_usage(retrieval_ms=4.5, stage_ms=4.0))
        )

        with self.assertRaisesRegex(RuntimeError, "metadata is inconsistent"):
            recorder.execute(_envelope())

    def test_rejects_missing_duplicate_and_out_of_bounds_evidence(self) -> None:
        recorder = _RetrievalStageRecorder(_Backend(_usage()))
        recorder.execute(_envelope())
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            recorder.execute(_envelope())
        with self.assertRaisesRegex(RuntimeError, "missing measured requests"):
            recorder.samples_for({"request-2"})

        short_row = _row()
        short_row["completed_monotonic_ms"] = 104.0
        with self.assertRaisesRegex(RuntimeError, "outside its HTTP request"):
            _attach_retrieval_stage_samples([short_row], recorder)

    def test_non_query_operations_do_not_produce_retrieval_samples(self) -> None:
        recorder = _RetrievalStageRecorder(_Backend(UsageMetadata()))
        recorder.execute(_envelope(operation=OperationKind.READINESS))

        with self.assertRaisesRegex(RuntimeError, "missing measured requests"):
            recorder.samples_for({"request-1"})

    def test_report_builder_requires_exact_independent_stage_coverage(self) -> None:
        request = {**_row(), "retrieval_stage_ms": 4.5}
        sample = {
            "request_id": "request-1",
            "retrieval_stage_ms": 4.5,
            "trace_id": "trace-request-1",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retrieval-stage.jsonl"
            path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
            self.assertEqual(_retrieval_stage_samples(path, [request]), [sample])

            sample["trace_id"] = "different-trace"
            path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "trace ID does not match"):
                _retrieval_stage_samples(path, [request])

            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                _retrieval_stage_samples(path, [request])

        raw_inspection = {
            "actual_neo4j_image": "neo4j:5.26.12-community",
            "actual_neo4j_repo_digest": "1" * 64,
            "code_commit": "a" * 40,
            "database_initial_node_count": 0,
            "database_initial_relationship_count": 0,
            "schema_version": "production-container-inspection-v2",
        }
        normalized = _container_inspection_projection(
            raw_inspection,
            code_commit="a" * 40,
        )
        self.assertEqual(normalized["code_commit"], "a" * 40)
        self.assertEqual(
            normalized["actual_neo4j_repo_digest"], "sha256:" + "1" * 64
        )
        for field, value, message in (
            ("schema_version", "production-container-inspection-v1", "version"),
            ("code_commit", "b" * 40, "code commit"),
            ("actual_neo4j_repo_digest", "not-a-digest", "RepoDigest"),
            ("actual_neo4j_repo_digest", None, "RepoDigest"),
            ("database_initial_node_count", True, "node_count"),
            ("database_initial_relationship_count", -1, "relationship_count"),
        ):
            with self.subTest(container_inspection_field=field):
                changed = {**raw_inspection, field: value}
                with self.assertRaisesRegex(ValueError, message):
                    _container_inspection_projection(
                        changed,
                        code_commit="a" * 40,
                    )
        for changed in (
            {key: value for key, value in raw_inspection.items() if key != "code_commit"},
            {**raw_inspection, "unexpected": 1},
        ):
            with self.assertRaisesRegex(ValueError, "schema is invalid"):
                _container_inspection_projection(
                    changed,
                    code_commit="a" * 40,
                )

        load_window = {
            "answer_samples": 30,
            "answer_warmup_requests": 30,
            "configured_sustained_seconds": 300,
            "forbidden_chunk_count": 4_500,
            "http_port": 8_000,
            "primary_tenant_active_chunks": 10_000,
            "readiness_probe_status": "ready",
            "readiness_transaction_timeout_seconds": 5,
            "retrieval_samples": 2_500,
            "retrieval_transaction_timeout_seconds": 5,
            "semantic_failure_count": 0,
            "schema_version": "production-load-window-v2",
            "warmup_requests": 16,
        }
        timeout_config = {
            "answer": {"warmup_requests": 30},
            "neo4j": {"online_transaction_timeout_seconds": 5},
            "retrieval": {"warmup_requests": 16},
        }
        self.assertEqual(
            _load_window_timeouts(load_window, timeout_config),
            (5.0, 5.0, "ready"),
        )
        with self.assertRaisesRegex(ValueError, "readiness probe"):
            _load_window_timeouts(
                {**load_window, "readiness_probe_status": "not_ready"},
                timeout_config,
            )
        with self.assertRaisesRegex(ValueError, "retrieval warmup"):
            _load_window_timeouts(
                {**load_window, "warmup_requests": 15},
                timeout_config,
            )
        with self.assertRaisesRegex(ValueError, "retrieval warmup"):
            _load_window_timeouts(
                {**load_window, "warmup_requests": True},
                timeout_config,
            )
        for invalid_answer_warmups in (29, True):
            with self.subTest(load_window_answer_warmups=invalid_answer_warmups):
                with self.assertRaisesRegex(ValueError, "answer preflight"):
                    _load_window_timeouts(
                        {
                            **load_window,
                            "answer_warmup_requests": invalid_answer_warmups,
                        },
                        timeout_config,
                    )


class ProviderTimeoutEvidenceTests(unittest.TestCase):
    def test_production_timeout_contract_uses_the_five_second_api_boundary(self) -> None:
        config = json.loads(
            (
                Path(__file__).parents[2]
                / "evaluation"
                / "production-reference-config.v1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(config["api"]["timeout_seconds"], 5)
        driver = Mock()
        driver.execute_query.return_value = ([{"ready": 1}], None, None)
        readiness = _Readiness(
            driver,
            "neo4j",
            transaction_timeout_seconds=config["neo4j"][
                "online_transaction_timeout_seconds"
            ],
        )
        self.assertEqual(readiness.transaction_timeout_seconds, 5.0)
        self.assertEqual(readiness.query.timeout, 5.0)
        self.assertEqual(
            readiness.query.metadata,
            {
                "component": "graphrag-readiness",
                "operation": "readiness",
            },
        )
        ready_result = readiness.check()
        self.assertEqual(ready_result.payload.status, "ready")
        driver.execute_query.assert_called_once_with(
            readiness.query,
            database_="neo4j",
            routing_=neo4j.RoutingControl.READ,
        )
        with patch(
            "scripts.run_production_load.httpx.get",
            return_value=httpx.Response(
                200,
                json={"status": "ready", "checks": {"neo4j": "ok"}},
            ),
        ) as probe:
            self.assertTrue(_probe_reference_readiness("http://127.0.0.1:8000"))
            probe.assert_called_once_with(
                "http://127.0.0.1:8000/health/ready",
                timeout=1,
            )
        fault_engines = _neo4j_fault_engines(
            server_driver=Mock(),
            unavailable_driver=Mock(),
            database="neo4j",
            online_timeout_seconds=5,
        )
        self.assertEqual(
            {
                mode: engine.transaction_timeout_seconds
                for mode, engine, _status, _code, _domain in fault_engines
            },
            {
                "failure": 5.0,
                "success": 5.0,
                "timeout": 0.001,
                "unavailable": 5.0,
            },
        )
        for invalid_timeout in (True, 0, float("inf"), 301):
            with self.subTest(readiness_timeout=invalid_timeout):
                with self.assertRaisesRegex(ValueError, "transaction_timeout_seconds"):
                    _Readiness(
                        object(),  # type: ignore[arg-type]
                        "neo4j",
                        transaction_timeout_seconds=invalid_timeout,
        )
        self.assertEqual(_answer_warmup_requests(config), 30)
        answer_body = _answer_body(config, "Which facts are supported?")
        self.assertEqual(
            answer_body["retrieval_limits"],
            asdict(PRODUCTION_ANSWER_RETRIEVAL_LIMITS),
        )
        self.assertEqual(
            resolve_production_answer_retrieval_limits(config),
            PRODUCTION_ANSWER_RETRIEVAL_LIMITS,
        )
        self.assertNotEqual(
            answer_body["retrieval_limits"]["seed_k"],
            config["retrieval"]["limits"]["seed_k"],
        )
        self.assertEqual(config["retrieval"]["limits"]["top_k"], 5)
        self.assertEqual(config["retrieval"]["limits"]["anchor_k"], 2)
        invalid_answer_limits = json.loads(json.dumps(config))
        invalid_answer_limits["answer"]["retrieval_limits"]["top_k"] = 5
        with self.assertRaisesRegex(RuntimeError, "not reviewed"):
            _answer_body(invalid_answer_limits, "Which facts are supported?")
        for mutation in ("missing", "extra", "invalid"):
            with self.subTest(answer_profile_mutation=mutation):
                changed = json.loads(json.dumps(config))
                if mutation == "missing":
                    del changed["answer"]["retrieval_limits"]["seed_k"]
                elif mutation == "extra":
                    changed["answer"]["retrieval_limits"]["unexpected"] = 1
                else:
                    changed["answer"]["retrieval_limits"]["seed_k"] = True
                with self.assertRaisesRegex(
                    ValueError,
                    "fully resolved|invalid",
                ):
                    resolve_production_answer_retrieval_limits(changed)
        for invalid_warmups in (0, 29, True):
            with self.subTest(answer_warmup_requests=invalid_warmups):
                changed = json.loads(json.dumps(config))
                changed["answer"]["warmup_requests"] = invalid_warmups
                with self.assertRaisesRegex(RuntimeError, "preflight all 30"):
                    _answer_warmup_requests(changed)
        failed_answer = {
            "case_id": "graph_relationship-boundary-02",
            "domain_failure_code": None,
            "domain_status": "insufficient_context",
            "error_code": None,
            "expected_chunk_ids": ["expected-atc", "expected-atl"],
            "inactive_chunk_ids": [],
            "inactive_version_count": 0,
            "selected_chunk_ids": ["expected-atl"],
            "semantic_success": False,
            "status_code": 200,
            "unauthorized_chunk_count": 0,
            "unauthorized_chunk_ids": [],
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "graph_relationship-boundary-02.*insufficient_context.*expected-atl",
        ):
            _require_reference_answer(failed_answer, phase="answer preflight")
        answer_queries = tuple(
            {"case_id": f"answer-{index:02d}"} for index in range(30)
        )
        expected_preflights = tuple(query["case_id"] for query in answer_queries)
        self.assertEqual(
            _require_answer_preflight_coverage(
                reversed(expected_preflights),
                answer_queries,
            ),
            tuple(sorted(expected_preflights)),
        )
        for incomplete in (
            expected_preflights[:-1],
            expected_preflights[:-1] + (expected_preflights[0],),
        ):
            with self.subTest(incomplete_preflight=incomplete[-1]):
                with self.assertRaisesRegex(RuntimeError, "exactly once"):
                    _require_answer_preflight_coverage(incomplete, answer_queries)
        for provider_id in ("embedding_provider", "llm_provider"):
            with self.subTest(provider_id=provider_id):
                self.assertEqual(
                    _provider_timeout_window_ms(config, provider_id),
                    (4_900.0, 6_000.0),
                )

    def test_configured_provider_delay_outlives_api_timeout_window(self) -> None:
        config = {
            "api": {
                "timeout_seconds": 0.02,
                "timeout_observation_early_tolerance_ms": 5,
                "timeout_observation_late_tolerance_ms": 10,
            },
            "dependencies": {
                "embedding_provider": {"timeout_delay_ms": 50},
                "llm_provider": {"timeout_delay_ms": 60},
            },
        }

        for provider_id in ("embedding_provider", "llm_provider"):
            with self.subTest(provider_id=provider_id):
                self.assertEqual(
                    _provider_timeout_window_ms(config, provider_id),
                    (15.0, 30.0),
                )
                self.assertEqual(
                    _report_timeout_window_ms(config, provider_id),
                    (15.0, 30.0),
                )

        rows = {
            "embedding_provider_timeout": {
                "finished_ns": 20_000_000,
                "started_ns": 0,
            },
            "llm_timeout": {"finished_ns": 25_000_000, "started_ns": 0},
        }
        _validate_provider_timeout_scenarios(config, rows)
        rows["llm_timeout"]["finished_ns"] = 10_000_000
        with self.assertRaisesRegex(ValueError, "configured API deadline"):
            _validate_provider_timeout_scenarios(config, rows)

        invalid = {
            **config,
            "dependencies": {
                **config["dependencies"],
                "embedding_provider": {"timeout_delay_ms": 30},
            },
        }
        with self.assertRaisesRegex(ValueError, "must outlive"):
            _provider_timeout_window_ms(invalid, "embedding_provider")

    def test_embedding_and_llm_stubs_are_cut_off_by_runtime_deadline(self) -> None:
        delay_ms = 200.0
        embedder = _ReferenceQueryEmbedder(
            {("tenant-1", "Question?"): ((1.0,), "space-1")},
            delay_ms=0.0,
            timeout_delay_ms=delay_ms,
        )
        embedder.set_mode("timeout")
        model = _ReferenceAnswerModel(
            delay_ms=0.0,
            timeout_delay_ms=delay_ms,
            predictions_by_query={},
        )
        generation = _MeteredReferenceGeneration(
            model,
            input_tokens=1,
            output_tokens=1,
            request_cost_usd=0.0,
        )
        generation.set_mode("timeout")

        async def exercise(backend: Backend, operation: OperationKind) -> float:
            runner = BoundedOperationRunner(
                backend,
                policy=RuntimePolicy(
                    max_workers=1,
                    max_queue_size=0,
                    max_attempts=1,
                    timeout_seconds=0.02,
                ),
            )
            started = time.monotonic()
            try:
                with self.assertRaises(DependencyTimeoutError):
                    await runner.run(_envelope(operation=operation))
                return time.monotonic() - started
            finally:
                await runner.aclose(wait=True)

        embedding_elapsed = asyncio.run(
            exercise(_EmbeddingBackend(embedder), OperationKind.RETRIEVAL)
        )
        llm_elapsed = asyncio.run(
            exercise(_GenerationBackend(generation), OperationKind.ANSWER)
        )

        self.assertLess(embedding_elapsed, delay_ms / 1_000.0)
        self.assertLess(llm_elapsed, delay_ms / 1_000.0)
        self.assertEqual(embedder.successful_calls, 1)
        self.assertEqual(generation.metered_calls, 1)
        self.assertTrue(embedder.wait_for_idle(0.01))
        self.assertTrue(generation.wait_for_idle(0.01))
        self.assertEqual(embedder.active_calls, 0)
        self.assertEqual(generation.active_calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
