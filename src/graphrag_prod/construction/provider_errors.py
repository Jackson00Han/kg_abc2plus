"""Safe, typed classifications for recoverable extraction-provider failures.

Only stable finding codes cross the construction/API boundary. Provider error
messages may contain request or source data and are deliberately not inspected.
"""

from __future__ import annotations

try:
    from openai import APITimeoutError
except ImportError:  # Provider-neutral adapters can be used without the SDK.
    _SDK_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _SDK_TIMEOUT_ERRORS = (APITimeoutError,)


MODEL_CALL_TIMEOUT = "MODEL_CALL_TIMEOUT"
MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
RETRYABLE_PROVIDER_FINDING_CODES = frozenset(
    {MODEL_CALL_TIMEOUT, MODEL_CALL_FAILED}
)


def provider_failure_code(error: BaseException) -> str:
    """Classify an actual timeout without interpreting arbitrary error text."""

    if isinstance(error, (TimeoutError, *_SDK_TIMEOUT_ERRORS)):
        return MODEL_CALL_TIMEOUT
    return MODEL_CALL_FAILED
