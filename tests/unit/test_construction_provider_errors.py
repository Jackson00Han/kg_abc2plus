"""Typed provider failures must retain timeout semantics without message parsing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APITimeoutError

from graphrag_prod.construction.provider_errors import (
    MODEL_CALL_FAILED,
    MODEL_CALL_TIMEOUT,
    RETRYABLE_PROVIDER_FINDING_CODES,
    provider_failure_code,
)


class ConstructionProviderErrorTests(unittest.TestCase):
    def test_builtin_and_sdk_timeouts_have_a_distinct_retryable_code(self) -> None:
        request = httpx.Request("POST", "https://provider.invalid/chat/completions")
        for error in (TimeoutError("private request detail"), APITimeoutError(request)):
            with self.subTest(error=type(error).__name__):
                code = provider_failure_code(error)
                self.assertEqual(code, MODEL_CALL_TIMEOUT)
                self.assertIn(code, RETRYABLE_PROVIDER_FINDING_CODES)
                self.assertNotIn("private", code)

    def test_non_timeout_errors_are_not_classified_by_name_or_message(self) -> None:
        lookalike = type("APITimeoutError", (Exception,), {})
        request = httpx.Request("POST", "https://provider.invalid/chat/completions")
        failures = (
            RuntimeError("APITimeoutError: timed out after 30 seconds"),
            lookalike("timeout"),
            APIConnectionError(request=request),
        )
        for error in failures:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(provider_failure_code(error), MODEL_CALL_FAILED)
        self.assertIn(MODEL_CALL_FAILED, RETRYABLE_PROVIDER_FINDING_CODES)

    def test_builtin_timeout_works_without_optional_sdk_types(self) -> None:
        with patch(
            "graphrag_prod.construction.provider_errors._SDK_TIMEOUT_ERRORS", ()
        ):
            self.assertEqual(provider_failure_code(TimeoutError()), MODEL_CALL_TIMEOUT)
            self.assertEqual(provider_failure_code(RuntimeError()), MODEL_CALL_FAILED)


if __name__ == "__main__":
    unittest.main()
