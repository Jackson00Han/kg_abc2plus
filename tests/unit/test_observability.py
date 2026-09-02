"""Unit tests for bounded, content-free operational telemetry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import StringIO
import json
import unittest

from graphrag_prod.observability.logging import (
    LOG_FIELD_ALLOWLIST,
    StructuredJsonLogger,
    build_log_record,
    json_log_line,
)
from graphrag_prod.observability.metrics import MetricsRegistry
from graphrag_prod.observability.redaction import (
    REDACTED,
    TRUNCATED,
    is_sensitive_key,
    redact_sensitive,
)


_FIXED_TIME = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)


class RedactionTests(unittest.TestCase):
    def test_recursive_redaction_covers_credentials_and_protected_content(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature1234"
        payload = {
            "Authorization": "Bearer top-secret-token",
            "clientSecret": "client-secret-value",
            "nested": {
                "api_key": "api-key-value",
                "url": "neo4j://dbuser:dbpassword@localhost:7687",
                "note": f"upstream returned {jwt}",
            },
            "query": "What protected revenue was reported?",
            "chunks": ["confidential chunk contents"],
            "safe": "request completed",
        }

        redacted = redact_sensitive(payload)
        serialized = json.dumps(redacted, sort_keys=True)

        for secret in (
            "top-secret-token",
            "client-secret-value",
            "api-key-value",
            "dbuser",
            "dbpassword",
            jwt,
            "protected revenue",
            "confidential chunk",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(redacted["Authorization"], REDACTED)
        self.assertEqual(redacted["clientSecret"], REDACTED)
        self.assertEqual(redacted["query"], REDACTED)
        self.assertEqual(redacted["chunks"], REDACTED)
        self.assertEqual(redacted["safe"], "request completed")

    def test_value_patterns_and_controls_are_sanitized_without_sensitive_key(self) -> None:
        value = (
            "line-one\r\nline-two "
            "password=hunter2 "
            "https://user:secret@example.com/path?api_key=abc123xyz"
        )
        result = redact_sensitive(value)
        self.assertNotIn("\r", result)
        self.assertNotIn("\n", result)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("user", result)
        self.assertNotIn("secret@example", result)
        self.assertNotIn("abc123xyz", result)

    def test_common_camel_case_secret_keys_are_sensitive(self) -> None:
        self.assertTrue(is_sensitive_key("apiKey"))
        self.assertTrue(is_sensitive_key("accessToken"))
        self.assertTrue(is_sensitive_key("privateKey"))
        self.assertTrue(is_sensitive_key("to\nken"))
        self.assertFalse(is_sensitive_key("request_id"))
        self.assertFalse(is_sensitive_key("chunk_id"))
        malformed = redact_sensitive({"to\nken": "must-not-leak"})
        self.assertNotIn("must-not-leak", json.dumps(malformed))

    def test_recursive_and_oversized_values_are_bounded(self) -> None:
        cycle: list[object] = []
        cycle.append(cycle)
        self.assertEqual(redact_sensitive(cycle), [REDACTED])
        result = redact_sensitive(
            {f"key-{index}": index for index in range(5)}, max_items=2
        )
        self.assertEqual(result[TRUNCATED], 3)
        self.assertTrue(str(redact_sensitive("abcdefgh", max_string_length=4)).endswith(TRUNCATED))


class StructuredLoggingTests(unittest.TestCase):
    def test_record_uses_allowlist_and_drops_all_content_fields(self) -> None:
        record = build_log_record(
            "INFO",
            "request.completed",
            timestamp=_FIXED_TIME,
            request_id="req-1",
            trace_id="trace-1",
            route="/answer?question=private",
            method="post",
            status=200,
            error_code=None,
            duration_ms=12.25,
            query="private question",
            body={"password": "private-password"},
            source_text="protected source",
            chunk="protected chunk",
            arbitrary="must not be logged",
        )

        self.assertEqual(record["timestamp"], "2026-09-02T01:02:03Z")
        self.assertEqual(record["route"], "/answer")
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["duration_ms"], 12.25)
        self.assertTrue(set(record) <= LOG_FIELD_ALLOWLIST | {"timestamp", "level", "service", "event"})
        serialized = json.dumps(record)
        for protected in (
            "private question",
            "private-password",
            "protected source",
            "protected chunk",
            "must not be logged",
        ):
            self.assertNotIn(protected, serialized)

    def test_logger_emits_exactly_one_json_line_and_blocks_log_injection(self) -> None:
        stream = StringIO()
        logger = StructuredJsonLogger(
            stream,
            clock=lambda: _FIXED_TIME,
            service="test-service",
        )
        record = logger.error(
            "request\r\nforged.completed",
            request_id="req-1\nFORGED LOG",
            route="/answer\nFORGED",
            method="POST",
            status=503,
            error_code="provider_error password=do-not-log",
            duration_ms=5,
            prompt="protected prompt",
        )

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), record)
        self.assertNotIn("do-not-log", lines[0])
        self.assertNotIn("protected prompt", lines[0])
        self.assertNotIn("\n", record["request_id"])
        self.assertNotIn("\r", record["event"])
        self.assertEqual(record["event"], "unknown")
        self.assertEqual(record["request_id"], REDACTED)
        self.assertEqual(record["error_code"], "unknown_error")

    def test_json_log_line_is_deterministic_for_a_fixed_timestamp(self) -> None:
        arguments = {
            "timestamp": _FIXED_TIME,
            "request_id": "request-a",
            "route": "/health",
            "method": "GET",
            "status": 200,
            "duration_ms": 1.25,
        }
        first = json_log_line("INFO", "request.completed", **arguments)
        second = json_log_line("INFO", "request.completed", **arguments)
        self.assertEqual(first, second)
        self.assertEqual(len(first.splitlines()), 1)


class MetricsRegistryTests(unittest.TestCase):
    def test_concurrent_updates_are_exact_and_snapshot_is_json_ready(self) -> None:
        registry = MetricsRegistry()
        worker_count = 8
        iterations = 250

        def worker(_: int) -> None:
            for _ in range(iterations):
                registry.record_request("/retrieve", "POST", 200, 2.5)
                registry.record_retrieval_stage("vector", 1.0)
                registry.record_model_call(10, 2, 0.0001)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(worker, range(worker_count)))

        expected = worker_count * iterations
        snapshot = registry.snapshot()
        self.assertEqual(snapshot["requests"]["total"], expected)
        self.assertEqual(snapshot["requests"]["by_route"]["POST /retrieve"]["count"], expected)
        self.assertEqual(snapshot["retrieval"]["by_stage"]["vector"]["count"], expected)
        self.assertEqual(snapshot["model"]["calls"], expected)
        self.assertEqual(snapshot["model"]["input_tokens"], expected * 10)
        self.assertEqual(snapshot["model"]["output_tokens"], expected * 2)
        self.assertAlmostEqual(snapshot["model"]["estimated_cost_usd"], expected * 0.0001)
        json.dumps(snapshot, sort_keys=True)
        self.assertEqual(snapshot, registry.snapshot())

    def test_label_cardinality_is_bounded_with_overflow_buckets(self) -> None:
        registry = MetricsRegistry(
            max_routes=2,
            max_error_codes=2,
            max_retrieval_stages=2,
        )
        registry.record_request("/a?query=private", "GET", 200, 1)
        registry.record_request("/a", "GET", 500, 2)
        registry.record_request("/b", "POST", 200, 3)
        registry.record_request("/c", "DELETE", 404, 4)
        for label in ("error-a", "error-b", "error-c"):
            registry.record_error(label)
        for stage in ("vector", "bm25", "attacker-controlled-stage"):
            registry.record_retrieval_stage(stage, 1)

        snapshot = registry.snapshot()
        self.assertEqual(
            set(snapshot["requests"]["by_route"]),
            {"GET /a", "POST /b", "DELETE <overflow>"},
        )
        self.assertEqual(snapshot["requests"]["error_count"], 2)
        self.assertEqual(len(snapshot["errors"]["by_code"]), 3)
        self.assertEqual(snapshot["errors"]["by_code"]["<overflow>"], 1)
        self.assertEqual(len(snapshot["retrieval"]["by_stage"]), 3)
        self.assertNotIn("private", json.dumps(snapshot))

    def test_usage_and_cost_accumulate_without_content_or_high_cardinality_labels(self) -> None:
        registry = MetricsRegistry()
        registry.record_model_usage(
            input_tokens=100,
            output_tokens=25,
            estimated_cost_usd=0.001,
        )
        registry.record_model_call(50, 10, 0.002)
        snapshot = registry.snapshot()["model"]
        self.assertEqual(
            snapshot,
            {
                "calls": 2,
                "input_tokens": 150,
                "output_tokens": 35,
                "estimated_cost_usd": 0.003,
            },
        )
        self.assertNotIn("model_id", snapshot)
        self.assertNotIn("prompt", snapshot)

        registry.record_model_usage(
            model_calls=2,
            input_tokens=20,
            output_tokens=4,
            estimated_cost_usd=0.004,
        )
        self.assertEqual(registry.snapshot()["model"]["calls"], 4)

    def test_invalid_observations_are_rejected(self) -> None:
        registry = MetricsRegistry()
        with self.assertRaises(ValueError):
            registry.record_request("/answer", "POST", 99, 1)
        with self.assertRaises(ValueError):
            registry.record_request("/answer", "POST", 200, float("nan"))
        with self.assertRaises(ValueError):
            registry.record_model_call(-1, 0, 0)
        with self.assertRaises(ValueError):
            registry.record_model_call(0, 0, float("inf"))
        with self.assertRaises(ValueError):
            registry.record_model_call(1_000_000_001, 0, 0)
        with self.assertRaises(ValueError):
            registry.record_model_usage(
                model_calls=0,
                input_tokens=1,
            )


if __name__ == "__main__":
    unittest.main()
