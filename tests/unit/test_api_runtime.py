"""Unit tests for bounded API execution, retry, and rate limiting."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from typing import Any

from graphrag_prod.api.runtime import (
    ApiRuntimeError,
    BackendResult,
    BoundedOperationRunner,
    DependencyTimeoutError,
    ErrorCode,
    OperationEnvelope,
    OperationKind,
    PrincipalRateLimiter,
    RateLimitAlgorithm,
    RateLimitExceeded,
    RateLimitPolicy,
    RetryableBackendError,
    RuntimeClosedError,
    RuntimeOverloadedError,
    RuntimePolicy,
    UsageMetadata,
    classify_exception,
    required_scope,
)


def _envelope(operation: OperationKind = OperationKind.RETRIEVAL) -> OperationEnvelope:
    return OperationEnvelope(
        operation=operation,
        request_id="req-1",
        trace_id="trace-1",
        principal_id="principal-1",
        tenant_id="tenant-a",
        access_groups=frozenset({"analyst"}),
        scopes=frozenset({required_scope(operation)}),
        payload={"query": "bounded retrieval"},
    )


class _ScriptedBackend:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.lock = threading.Lock()

    def execute(self, envelope: OperationEnvelope) -> BackendResult:
        del envelope
        with self.lock:
            self.calls += 1
            outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


class RuntimeContractTests(unittest.TestCase):
    def test_envelope_normalizes_identity_and_freezes_payload(self) -> None:
        payload = {"key": "value"}
        envelope = OperationEnvelope(
            operation="retrieval",
            request_id=" req ",
            trace_id=" trace ",
            principal_id=" principal ",
            tenant_id=" tenant ",
            access_groups=frozenset({" group "}),
            scopes=frozenset({" retrieval:read "}),
            payload=payload,
        )
        payload["key"] = "changed"
        self.assertIs(envelope.operation, OperationKind.RETRIEVAL)
        self.assertEqual(envelope.principal_id, "principal")
        self.assertEqual(envelope.access_groups, frozenset({"group"}))
        self.assertEqual(envelope.scopes, frozenset({"retrieval:read"}))
        self.assertEqual(envelope.payload["key"], "value")
        with self.assertRaises(TypeError):
            envelope.payload["new"] = "forbidden"  # type: ignore[index]

    def test_envelope_and_usage_reject_invalid_values(self) -> None:
        values = {
            "request_id": "",
            "trace_id": "bad\ntrace",
            "principal_id": " ",
            "tenant_id": "\x00",
        }
        for field_name, value in values.items():
            kwargs = {
                "operation": OperationKind.RETRIEVAL,
                "request_id": "request",
                "trace_id": "trace",
                "principal_id": "principal",
                "tenant_id": "tenant",
            }
            kwargs[field_name] = value
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    OperationEnvelope(**kwargs)
        with self.assertRaises(ValueError):
            UsageMetadata(total_ms=float("nan"))
        with self.assertRaises(ValueError):
            UsageMetadata(input_tokens=-1)
        with self.assertRaises(ValueError):
            UsageMetadata(stages=(("recall", 1.0), ("recall", 2.0)))

    def test_operation_write_boundary_is_explicit(self) -> None:
        self.assertTrue(OperationKind.INGESTION.is_write)
        self.assertTrue(OperationKind.DELETION.is_write)
        for operation in (
            OperationKind.JOB_STATUS,
            OperationKind.RETRIEVAL,
            OperationKind.ANSWER,
            OperationKind.HEALTH,
            OperationKind.READINESS,
        ):
            self.assertFalse(operation.is_write)
        self.assertFalse(OperationKind.ANSWER.is_retry_safe)
        self.assertFalse(OperationKind.INGESTION.is_retry_safe)
        self.assertFalse(OperationKind.DELETION.is_retry_safe)
        self.assertFalse(OperationKind.RETRIEVAL.is_retry_safe)
        for operation in (
            OperationKind.JOB_STATUS,
            OperationKind.HEALTH,
            OperationKind.READINESS,
        ):
            self.assertTrue(operation.is_retry_safe)

    def test_runtime_policy_validates_every_bound(self) -> None:
        invalid = (
            {"max_workers": 0},
            {"max_queue_size": -1},
            {"timeout_seconds": 0},
            {"max_attempts": 0},
            {"initial_backoff_seconds": -1},
            {"max_backoff_seconds": -1},
            {"initial_backoff_seconds": 2, "max_backoff_seconds": 1},
            {"overload_retry_after_seconds": 0},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RuntimePolicy(**kwargs)

    def test_error_classification_is_stable_and_redacts_unknown_errors(self) -> None:
        classified = classify_exception(
            RateLimitExceeded(retry_after_seconds=2.5)
        )
        self.assertEqual(classified.code, ErrorCode.RATE_LIMITED)
        self.assertEqual(classified.status_code, 429)
        self.assertEqual(classified.retry_after_seconds, 2.5)
        secret = "sensitive-exception-detail"
        unknown = classify_exception(RuntimeError(secret))
        self.assertEqual(unknown.code, ErrorCode.INTERNAL)
        self.assertNotIn(secret, unknown.public_message)
        timeout = classify_exception(TimeoutError("private dependency address"))
        self.assertEqual(timeout.code, ErrorCode.DEPENDENCY_TIMEOUT)
        self.assertNotIn("private", timeout.public_message)
        custom = classify_exception(
            RetryableBackendError("password=audit-leak-value")
        )
        self.assertEqual(custom.code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertNotIn("audit-leak-value", custom.public_message)

    def test_api_error_rejects_invalid_retry_after(self) -> None:
        with self.assertRaises(ValueError):
            ApiRuntimeError(retry_after_seconds=0)


class BoundedOperationRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runners: list[BoundedOperationRunner] = []

    async def asyncTearDown(self) -> None:
        for runner in self.runners:
            await runner.aclose(wait=True)

    def _runner(
        self,
        backend: Any,
        *,
        policy: RuntimePolicy,
        sleeper: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
    ) -> BoundedOperationRunner:
        runner = BoundedOperationRunner(
            backend,
            policy=policy,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        self.runners.append(runner)
        return runner

    async def test_worker_and_submission_queue_are_both_hard_bounded(self) -> None:
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        calls = 0

        class BlockingBackend:
            def execute(self, envelope: OperationEnvelope) -> BackendResult:
                nonlocal active, maximum_active, calls
                del envelope
                with state_lock:
                    calls += 1
                    active += 1
                    maximum_active = max(maximum_active, active)
                release.wait(timeout=2)
                with state_lock:
                    active -= 1
                return BackendResult({"ok": True})

        runner = self._runner(
            BlockingBackend(),
            policy=RuntimePolicy(
                max_workers=2,
                max_queue_size=1,
                timeout_seconds=1,
                max_attempts=1,
            ),
        )
        accepted = [
            asyncio.create_task(runner.run(_envelope())) for _ in range(3)
        ]
        for _ in range(100):
            with state_lock:
                if calls == 2:
                    break
            await asyncio.sleep(0.005)
        with self.assertRaises(RuntimeOverloadedError):
            await runner.run(_envelope())
        release.set()
        await asyncio.gather(*accepted)
        self.assertEqual(calls, 3)
        self.assertEqual(maximum_active, 2)

    async def test_timeout_does_not_release_capacity_before_thread_finishes(self) -> None:
        release = threading.Event()
        first_started = threading.Event()
        calls = 0
        lock = threading.Lock()

        class TimeoutBackend:
            def execute(self, envelope: OperationEnvelope) -> BackendResult:
                nonlocal calls
                del envelope
                with lock:
                    calls += 1
                    call = calls
                if call == 1:
                    first_started.set()
                    release.wait(timeout=2)
                return BackendResult({"call": call})

        runner = self._runner(
            TimeoutBackend(),
            policy=RuntimePolicy(
                max_workers=1,
                max_queue_size=0,
                timeout_seconds=0.03,
                max_attempts=3,
            ),
        )
        with self.assertRaises(DependencyTimeoutError):
            await runner.run(_envelope())
        self.assertTrue(first_started.is_set())
        with self.assertRaises(RuntimeOverloadedError):
            await runner.run(_envelope())
        self.assertEqual(calls, 1)
        release.set()
        for _ in range(100):
            try:
                result = await runner.run(_envelope())
            except RuntimeOverloadedError:
                await asyncio.sleep(0.005)
                continue
            break
        else:
            self.fail("capacity was not restored after the worker completed")
        self.assertEqual(result.payload, {"call": 2})

    async def test_cancellation_also_keeps_capacity_until_worker_finishes(self) -> None:
        release = threading.Event()
        started = threading.Event()

        class BlockingBackend:
            def execute(self, envelope: OperationEnvelope) -> BackendResult:
                del envelope
                started.set()
                release.wait(timeout=2)
                return BackendResult({"ok": True})

        runner = self._runner(
            BlockingBackend(),
            policy=RuntimePolicy(
                max_workers=1,
                max_queue_size=0,
                timeout_seconds=1,
                max_attempts=1,
            ),
        )
        pending = asyncio.create_task(runner.run(_envelope()))
        await asyncio.to_thread(started.wait, 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        with self.assertRaises(RuntimeOverloadedError):
            await runner.run(_envelope())
        release.set()

    async def test_only_explicit_retryable_backend_errors_retry_reads(self) -> None:
        backend = _ScriptedBackend(
            [
                RetryableBackendError(),
                RetryableBackendError(),
                BackendResult({"ok": True}),
            ]
        )
        delays: list[float] = []

        async def no_wait(delay: float) -> None:
            delays.append(delay)

        runner = self._runner(
            backend,
            policy=RuntimePolicy(
                max_workers=1,
                max_queue_size=0,
                timeout_seconds=1,
                max_attempts=5,
                initial_backoff_seconds=0.1,
                max_backoff_seconds=0.15,
            ),
            sleeper=no_wait,
        )
        result = await runner.run(_envelope(OperationKind.JOB_STATUS))
        self.assertEqual(result.payload, {"ok": True})
        self.assertEqual(backend.calls, 3)
        self.assertEqual(delays, [0.1, 0.15])

    async def test_unmarked_failures_never_retry(self) -> None:
        backend = _ScriptedBackend(
            [RuntimeError("not explicitly retryable"), BackendResult({"ok": True})]
        )
        runner = self._runner(
            backend,
            policy=RuntimePolicy(timeout_seconds=1, max_attempts=3),
        )
        with self.assertRaisesRegex(RuntimeError, "not explicitly retryable"):
            await runner.run(_envelope(OperationKind.ANSWER))
        self.assertEqual(backend.calls, 1)

    async def test_writes_never_retry_even_explicit_retryable_errors(self) -> None:
        for operation in (OperationKind.INGESTION, OperationKind.DELETION):
            backend = _ScriptedBackend(
                [RetryableBackendError(), BackendResult({"ok": True})]
            )
            runner = self._runner(
                backend,
                policy=RuntimePolicy(timeout_seconds=1, max_attempts=5),
            )
            with self.subTest(operation=operation):
                with self.assertRaises(RetryableBackendError):
                    await runner.run(_envelope(operation))
                self.assertEqual(backend.calls, 1)

    async def test_answer_never_retries_billable_provider_work(self) -> None:
        backend = _ScriptedBackend([RetryableBackendError()])
        runner = self._runner(
            backend,
            policy=RuntimePolicy(max_attempts=3),
        )
        with self.assertRaises(RetryableBackendError):
            await runner.run(_envelope(OperationKind.ANSWER))
        self.assertEqual(backend.calls, 1)

    async def test_read_timeout_is_not_retried_and_write_timeout_is_not_retried(
        self,
    ) -> None:
        for operation in (OperationKind.RETRIEVAL, OperationKind.INGESTION):
            release = threading.Event()
            calls = 0

            class SlowBackend:
                def execute(self, envelope: OperationEnvelope) -> BackendResult:
                    nonlocal calls
                    del envelope
                    calls += 1
                    release.wait(timeout=2)
                    return BackendResult({"ok": True})

            runner = self._runner(
                SlowBackend(),
                policy=RuntimePolicy(
                    max_workers=1,
                    max_queue_size=0,
                    timeout_seconds=0.02,
                    max_attempts=5,
                ),
            )
            with self.subTest(operation=operation):
                with self.assertRaises(DependencyTimeoutError):
                    await runner.run(_envelope(operation))
                self.assertEqual(calls, 1)
            release.set()

    async def test_runner_records_total_latency_without_overwriting_usage(self) -> None:
        ticks = iter((10.0, 10.0, 10.125))
        backend = _ScriptedBackend(
            [
                BackendResult(
                    {"ok": True},
                    UsageMetadata(retrieval_ms=40, model_calls=1),
                )
            ]
        )
        runner = self._runner(
            backend,
            policy=RuntimePolicy(timeout_seconds=1, max_attempts=1),
            monotonic=lambda: next(ticks),
        )
        result = await runner.run(_envelope())
        self.assertEqual(result.usage.total_ms, 125)
        self.assertEqual(result.usage.retrieval_ms, 40)
        self.assertEqual(result.usage.model_calls, 1)

    async def test_close_is_idempotent_and_rejects_new_work(self) -> None:
        runner = self._runner(
            _ScriptedBackend([BackendResult({"ok": True})]),
            policy=RuntimePolicy(timeout_seconds=1),
        )
        await runner.aclose()
        await runner.aclose()
        self.assertTrue(runner.closed)
        with self.assertRaises(RuntimeClosedError):
            await runner.run(_envelope())


class PrincipalRateLimiterTests(unittest.TestCase):
    def test_fixed_window_is_per_principal_and_returns_retry_after(self) -> None:
        now = [100.0]
        limiter = PrincipalRateLimiter(
            RateLimitPolicy(
                requests=2,
                window_seconds=10,
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
            ),
            monotonic=lambda: now[0],
        )
        self.assertTrue(limiter.require("alice").allowed)
        self.assertTrue(limiter.require("alice").allowed)
        denied = limiter.check("alice")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after_seconds, 10)
        self.assertTrue(limiter.require("bob").allowed)
        with self.assertRaises(RateLimitExceeded) as caught:
            limiter.require("alice")
        self.assertEqual(caught.exception.retry_after_seconds, 10)
        now[0] += 10
        self.assertTrue(limiter.require("alice").allowed)

    def test_token_bucket_refills_deterministically(self) -> None:
        now = [0.0]
        limiter = PrincipalRateLimiter(
            RateLimitPolicy(
                requests=2,
                window_seconds=4,
                algorithm=RateLimitAlgorithm.TOKEN_BUCKET,
                burst_capacity=2,
            ),
            monotonic=lambda: now[0],
        )
        limiter.require("alice")
        limiter.require("alice")
        denied = limiter.check("alice")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after_seconds, 2)
        now[0] = 2
        allowed = limiter.require("alice")
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.remaining, 0)

    def test_limiter_is_thread_safe(self) -> None:
        limiter = PrincipalRateLimiter(
            RateLimitPolicy(
                requests=25,
                window_seconds=60,
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
            ),
            monotonic=lambda: 1.0,
        )
        with ThreadPoolExecutor(max_workers=16) as executor:
            decisions = list(
                executor.map(lambda _: limiter.check("principal"), range(100))
            )
        self.assertEqual(sum(decision.allowed for decision in decisions), 25)

    def test_identity_capacity_fails_closed_until_state_is_stale(self) -> None:
        now = [0.0]
        limiter = PrincipalRateLimiter(
            RateLimitPolicy(
                requests=1,
                window_seconds=10,
                algorithm=RateLimitAlgorithm.FIXED_WINDOW,
                max_principals=1,
            ),
            monotonic=lambda: now[0],
        )
        limiter.require("alice")
        with self.assertRaises(RuntimeOverloadedError):
            limiter.require("bob")
        now[0] = 10
        self.assertTrue(limiter.require("bob").allowed)

    def test_close_clears_state_and_rejects_checks(self) -> None:
        limiter = PrincipalRateLimiter()
        limiter.require("alice")
        limiter.close()
        limiter.close()
        self.assertTrue(limiter.closed)
        with self.assertRaises(RuntimeClosedError):
            limiter.check("alice")

    def test_rate_limit_policy_and_cost_are_strictly_validated(self) -> None:
        invalid = (
            {"requests": 0},
            {"window_seconds": 0},
            {"algorithm": "sliding"},
            {"burst_capacity": 0},
            {
                "requests": 2,
                "algorithm": RateLimitAlgorithm.FIXED_WINDOW,
                "burst_capacity": 3,
            },
            {"max_principals": 0},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RateLimitPolicy(**kwargs)
        limiter = PrincipalRateLimiter(RateLimitPolicy(burst_capacity=2))
        with self.assertRaises(ValueError):
            limiter.check("alice", cost=3)


if __name__ == "__main__":
    unittest.main()
