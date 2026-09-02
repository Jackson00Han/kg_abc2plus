"""Canonical production-reference configuration projections.

The sustained retrieval workload intentionally uses tighter limits than answer
generation.  Answer callers must therefore resolve the complete reviewed
profile instead of overlaying a few fields on the load-test profile.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from graphrag_prod.retrieval import RetrievalLimits


PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION = "production-reference-config-v1"
PRODUCTION_REFERENCE_CONFIG_VERSION = "1.0.5"

PRODUCTION_ANSWER_RETRIEVAL_LIMITS = RetrievalLimits(
    top_k=10,
    anchor_k=5,
    minimum_vector_score=0.75,
)


def resolve_production_answer_retrieval_limits(
    config: Mapping[str, Any],
) -> RetrievalLimits:
    """Return the complete reviewed answer profile or fail closed.

    Requiring the fully resolved mapping prevents a partial overlay from
    silently inheriting the deliberately narrower sustained-load limits.
    """

    if (
        config.get("schema_version")
        != PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION
        or config.get("version") != PRODUCTION_REFERENCE_CONFIG_VERSION
        or config.get("profile_id") != "production-reference"
    ):
        raise ValueError("production-reference configuration identity is invalid")
    answer = config.get("answer")
    if not isinstance(answer, Mapping):
        raise ValueError("production answer configuration is missing")
    raw = answer.get("retrieval_limits")
    expected = asdict(PRODUCTION_ANSWER_RETRIEVAL_LIMITS)
    if not isinstance(raw, Mapping) or set(raw) != set(expected):
        raise ValueError("production answer retrieval profile must be fully resolved")
    try:
        resolved = RetrievalLimits(**dict(raw))
    except (TypeError, ValueError) as error:
        raise ValueError("production answer retrieval profile is invalid") from error
    if resolved != PRODUCTION_ANSWER_RETRIEVAL_LIMITS:
        raise ValueError("production answer retrieval profile is not reviewed")
    return resolved


__all__ = [
    "PRODUCTION_ANSWER_RETRIEVAL_LIMITS",
    "PRODUCTION_REFERENCE_CONFIG_SCHEMA_VERSION",
    "PRODUCTION_REFERENCE_CONFIG_VERSION",
    "resolve_production_answer_retrieval_limits",
]
