"""Grounded generation with server-owned citations and fail-closed validation."""

from .models import (
    OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
    REFUSAL_ANSWER,
    AnswerCitation,
    AnswerModel,
    AnswerModelRequest,
    AnswerResult,
    AnswerStatus,
    Claim,
    Conflict,
    GenerationLimits,
    GenerationRequest,
)
from .prompt import LabelledContext, build_prompt, label_context
from .service import (
    GENERATION_LIMIT_EXCEEDED,
    INVALID_CONTEXT,
    INVALID_MODEL_OUTPUT,
    GroundedGenerationService,
    UnsafeModelOutput,
)

__all__ = [
    "GENERATION_LIMIT_EXCEEDED",
    "INVALID_CONTEXT",
    "INVALID_MODEL_OUTPUT",
    "OUTPUT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "REFUSAL_ANSWER",
    "AnswerCitation",
    "AnswerModel",
    "AnswerModelRequest",
    "AnswerResult",
    "AnswerStatus",
    "Claim",
    "Conflict",
    "GenerationRequest",
    "GenerationLimits",
    "GroundedGenerationService",
    "LabelledContext",
    "UnsafeModelOutput",
    "build_prompt",
    "label_context",
]
