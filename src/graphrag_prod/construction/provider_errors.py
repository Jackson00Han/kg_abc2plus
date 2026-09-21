"""Safe, typed classifications for recoverable extraction-provider failures.

Only stable finding codes cross the construction/API boundary. Provider error
messages may contain request or source data and are deliberately not inspected.
"""

from __future__ import annotations

try:
    from openai import APIConnectionError, APIStatusError, APITimeoutError
except ImportError:  # Provider-neutral adapters can be used without the SDK.
    _SDK_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = ()
    _SDK_CONNECTION_ERRORS: tuple[type[BaseException], ...] = ()
    _SDK_STATUS_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _SDK_TIMEOUT_ERRORS = (APITimeoutError,)
    _SDK_CONNECTION_ERRORS = (APIConnectionError,)
    _SDK_STATUS_ERRORS = (APIStatusError,)


MODEL_CALL_TIMEOUT = "MODEL_CALL_TIMEOUT"
MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
MODEL_CONNECTION_ERROR = "MODEL_CONNECTION_ERROR"
MODEL_QUOTA_EXHAUSTED = "MODEL_QUOTA_EXHAUSTED"
MODEL_ACCOUNT_ARREARS = "MODEL_ACCOUNT_ARREARS"
MODEL_ACCESS_DENIED = "MODEL_ACCESS_DENIED"
RETRYABLE_PROVIDER_FINDING_CODES = frozenset(
    {MODEL_CALL_TIMEOUT, MODEL_CALL_FAILED, MODEL_CONNECTION_ERROR,
     MODEL_QUOTA_EXHAUSTED, MODEL_ACCOUNT_ARREARS, MODEL_ACCESS_DENIED}
)


def provider_failure_code(error: BaseException) -> str:
    """Use SDK types and allowlisted structured codes, never error messages.

    Quota/access failures remain resumable after configuration or account
    recovery; this classification does not itself issue another model call.
    """

    if isinstance(error, (TimeoutError, *_SDK_TIMEOUT_ERRORS)):
        return MODEL_CALL_TIMEOUT
    if isinstance(error, _SDK_CONNECTION_ERRORS):
        return MODEL_CONNECTION_ERROR
    if isinstance(error, _SDK_STATUS_ERRORS):
        body = error.body if isinstance(error.body, dict) else {}
        details = body.get("error", body)
        code = details.get("code") if isinstance(details, dict) else None
        if code == "Arrearage":
            return MODEL_ACCOUNT_ARREARS
        if code in ("insufficient_quota", "AllocationQuota.FreeTierOnly"):
            return MODEL_QUOTA_EXHAUSTED
        if error.status_code in (401, 403):
            return MODEL_ACCESS_DENIED
    return MODEL_CALL_FAILED
