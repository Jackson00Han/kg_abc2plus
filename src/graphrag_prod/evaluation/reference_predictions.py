"""Recorded deterministic answer predictions kept separate from runtime gold."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from graphrag_prod.generation import AnswerModelRequest


REFERENCE_PREDICTION_SCHEMA_VERSION = "reference-answer-predictions-v1"
REFERENCE_PREDICTION_VERSION = "1.0.0"
REFERENCE_PREDICTION_PROVIDER = "deterministic-recorded-answer-fixture"
REFERENCE_PREDICTION_SHA256 = (
    "7a8da52864de2be7e4f6b0700e43af1ae66270bd141e99ecf714f177997231c8"
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "case_count",
        "cases",
        "dataset_id",
        "dataset_version",
        "prediction_provider",
        "schema_version",
        "version",
    }
)
_CASE_FIELDS = frozenset({"claims", "id", "query", "status"})
_CLAIM_FIELDS = frozenset(
    {"evidence_chunk_ids", "inference", "text"}
)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def load_reference_predictions(path: Path) -> dict[str, dict[str, Any]]:
    """Load the pinned prediction asset without consulting answer gold."""

    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REFERENCE_PREDICTION_SHA256:
        raise ValueError("reference answer prediction bytes do not match the pin")
    value = json.loads(raw)
    if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL_FIELDS:
        raise ValueError("reference answer prediction fields are invalid")
    if (
        value["schema_version"] != REFERENCE_PREDICTION_SCHEMA_VERSION
        or value["version"] != REFERENCE_PREDICTION_VERSION
        or value["prediction_provider"] != REFERENCE_PREDICTION_PROVIDER
        or value["dataset_id"] != "dev-corpus-v1"
        or value["dataset_version"] != "1.0.1"
        or value["case_count"] != 49
    ):
        raise ValueError("reference answer prediction identity is stale")
    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or len(raw_cases) != 49:
        raise ValueError("reference answer predictions require exactly 49 cases")

    by_query: dict[str, dict[str, Any]] = {}
    case_ids: set[str] = set()
    for case_index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, Mapping) or set(raw_case) != _CASE_FIELDS:
            raise ValueError(f"reference prediction case {case_index} is invalid")
        case_id = _required_text(raw_case["id"], "reference prediction case ID")
        query = _required_text(raw_case["query"], "reference prediction query")
        status = _required_text(raw_case["status"], "reference prediction status")
        if status not in {"answered", "insufficient_context"}:
            raise ValueError("reference prediction status is unsupported")
        if case_id in case_ids or query in by_query:
            raise ValueError("reference prediction IDs and queries must be unique")
        raw_claims = raw_case["claims"]
        if not isinstance(raw_claims, list):
            raise ValueError("reference prediction claims must be a list")
        claims: list[dict[str, Any]] = []
        for claim_index, raw_claim in enumerate(raw_claims):
            if not isinstance(raw_claim, Mapping) or set(raw_claim) != _CLAIM_FIELDS:
                raise ValueError(
                    f"reference prediction {case_id} claim {claim_index} is invalid"
                )
            chunk_ids = raw_claim["evidence_chunk_ids"]
            if (
                not isinstance(chunk_ids, list)
                or not chunk_ids
                or len(set(chunk_ids)) != len(chunk_ids)
                or any(not isinstance(item, str) or not item for item in chunk_ids)
            ):
                raise ValueError("reference prediction evidence IDs are invalid")
            inference = raw_claim["inference"]
            if not isinstance(inference, bool):
                raise ValueError("reference prediction inference must be boolean")
            claims.append(
                {
                    "evidence_chunk_ids": list(chunk_ids),
                    "inference": inference,
                    "text": _required_text(
                        raw_claim["text"], "reference prediction claim text"
                    ),
                }
            )
        if (status == "answered") != bool(claims):
            raise ValueError("reference prediction status and claims disagree")
        case_ids.add(case_id)
        by_query[query] = {
            "claims": claims,
            "id": case_id,
            "status": status,
        }
    return by_query


def prediction_payload(
    request: AnswerModelRequest,
    predictions_by_query: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Render one recorded prediction against sources present in the prompt."""

    marker = "INPUT_JSON:\n"
    if marker not in request.prompt:
        raise RuntimeError("reference prompt marker is missing")
    prompt = json.loads(request.prompt.split(marker, 1)[1])
    if not isinstance(prompt, Mapping):
        raise RuntimeError("reference prompt input is invalid")
    question = _required_text(prompt.get("question"), "reference prompt question")
    prediction = predictions_by_query.get(question)
    if prediction is None:
        raise RuntimeError("reference prompt has no reviewed prediction")
    sources = prompt.get("sources")
    if not isinstance(sources, list):
        raise RuntimeError("reference prompt sources are invalid")
    sources_by_chunk: dict[str, Mapping[str, Any]] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            raise RuntimeError("reference prompt source is invalid")
        chunk_id = _required_text(source.get("chunk_id"), "reference source Chunk ID")
        if chunk_id in sources_by_chunk:
            raise RuntimeError("reference prompt repeats a source Chunk")
        sources_by_chunk[chunk_id] = source
    if prediction["status"] == "insufficient_context":
        return {"status": "insufficient_context", "claims": [], "conflicts": []}

    claims: list[dict[str, Any]] = []
    for claim in prediction["claims"]:
        evidence_chunk_ids = claim["evidence_chunk_ids"]
        selected = [
            sources_by_chunk[chunk_id]
            for chunk_id in evidence_chunk_ids
            if chunk_id in sources_by_chunk
        ]
        # A sourced claim may list interchangeable adjudicated sources, so one
        # present source is sufficient.  An inference lists its distinct
        # operands and remains fail-closed unless every operand is present.
        if claim["inference"]:
            evidence_complete = (
                len(selected) == len(evidence_chunk_ids) and len(selected) >= 2
            )
        else:
            evidence_complete = bool(selected)
        if not evidence_complete:
            return {"status": "insufficient_context", "claims": [], "conflicts": []}
        citation_ids = [
            _required_text(source.get("citation_id"), "reference citation ID")
            for source in selected
        ]
        claims.append(
            {
                "citation_ids": citation_ids,
                "evidence": [
                    {
                        "citation_id": citation_id,
                        "quote": _required_text(
                            source.get("text"), "reference evidence quote"
                        ),
                    }
                    for citation_id, source in zip(citation_ids, selected)
                ],
                "inference": bool(claim["inference"]),
                "material": True,
                "text": str(claim["text"]),
            }
        )
    return {"status": "answered", "claims": claims, "conflicts": []}


__all__ = [
    "REFERENCE_PREDICTION_PROVIDER",
    "REFERENCE_PREDICTION_SCHEMA_VERSION",
    "REFERENCE_PREDICTION_SHA256",
    "REFERENCE_PREDICTION_VERSION",
    "load_reference_predictions",
    "prediction_payload",
]
