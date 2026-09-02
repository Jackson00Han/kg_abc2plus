"""Prose-redacted, independently reproducible Stage 9 quality evidence.

The raw retrieval observations contain only stable resource identifiers.  Raw
answer observations replace every generated text and citation location with a
SHA-256 commitment.  The evaluator verifies those commitments against the
committed ``gold-v1`` artifacts before reconstructing the inputs accepted by
the Stage 8 metric implementations.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION
from graphrag_prod.retrieval.metrics import evaluate_retrieval_results

from .answers import (
    CITATION_LOCATION_FIELDS,
    STANDARD_REFUSAL_ANSWER,
    calculate_answer_metrics,
    evaluate_answer_results,
)


ROOT = Path(__file__).resolve().parents[3]
GOLD_MANIFEST = ROOT / "evaluation" / "gold-v1" / "manifest.json"
GOLD_QUESTIONS = ROOT / "evaluation" / "gold-v1" / "questions.jsonl"
GOLD_ANSWERS = ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl"

QUALITY_CASE_EVIDENCE_SCHEMA_VERSION = (
    "production-large-database-quality-cases-v1"
)
HTTP_ANSWER_COMMITMENT_SCHEMA_VERSION = "production-http-answer-commitment-v1"
EXPECTED_GOLD_DATASET_ID = "gold-v1"
EXPECTED_GOLD_VERSION = "2.0.0"
EXPECTED_QUESTION_SHA256 = (
    "d867f05095fa55ef4d9134f854cea4188d65a31be17c09483092386bd97f6f34"
)
EXPECTED_ANSWER_SHA256 = (
    "a18841f0a4624941fa3082eb1db38f63a6edfec687e9b569fa94e2968551e8c5"
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "answer_gold_sha256",
        "case_count",
        "case_ids",
        "cases",
        "dataset_id",
        "dataset_version",
        "retrieval_gold_sha256",
        "schema_version",
    }
)
_CASE_FIELDS = frozenset({"answer", "id", "retrieval"})
_RETRIEVAL_FIELDS = frozenset({"ranking", "visible_resources"})
_ANSWER_FIELDS = frozenset(
    {
        "answer_sha256",
        "citations",
        "claims",
        "conflicts",
        "failure_code",
        "output_schema_version",
        "prompt_version",
        "status",
    }
)
_CLAIM_FIELDS = frozenset(
    {"citation_ids", "claim_id", "inference", "material", "text_sha256"}
)
_CITATION_FIELDS = frozenset({"citation_id", "chunk_id", "location_sha256"})
_CONFLICT_FIELDS = frozenset({"claim_indexes", "topic_sha256"})
_HTTP_ANSWER_FIELDS = frozenset(
    {
        "answer_sha256",
        "citations",
        "claims",
        "conflicts",
        "failure_code",
        "output_schema_version",
        "prompt_version",
        "schema_version",
        "status",
    }
)
_HTTP_CLAIM_FIELDS = frozenset(
    {"citation_ids", "inference", "material", "text_sha256"}
)
_HTTP_CITATION_FIELDS = frozenset(
    {"citation_id", "chunk_id", "location_sha256", "version_id"}
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_quality_digest(value: object) -> str:
    """Return a prefixed digest over the canonical JSON evidence bytes."""
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must be a JSON object")
            rows.append(value)
    return rows


def _committed_gold() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load gold only after pinning both files to immutable known digests."""
    manifest = json.loads(GOLD_MANIFEST.read_text(encoding="utf-8"))
    if (
        manifest.get("dataset_id") != EXPECTED_GOLD_DATASET_ID
        or manifest.get("version") != EXPECTED_GOLD_VERSION
    ):
        raise ValueError("committed quality gold identity is stale")
    artifacts = {
        item.get("role"): item
        for item in manifest.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    expected = {
        "questions": (
            "evaluation/gold-v1/questions.jsonl",
            EXPECTED_QUESTION_SHA256,
            GOLD_QUESTIONS,
        ),
        "answers": (
            "datasets/dev-corpus-v1/answers.jsonl",
            EXPECTED_ANSWER_SHA256,
            GOLD_ANSWERS,
        ),
    }
    for role, (relative_path, digest, path) in expected.items():
        artifact = artifacts.get(role)
        if (
            artifact is None
            or artifact.get("path") != relative_path
            or artifact.get("item_count") != 49
            or artifact.get("sha256") != digest
            or _file_digest(path) != digest
        ):
            raise ValueError(f"committed quality {role} gold is not pinned")
    questions = _load_jsonl(GOLD_QUESTIONS)
    answers = _load_jsonl(GOLD_ANSWERS)
    if len(questions) != 49 or len(answers) != 49:
        raise ValueError("committed quality gold must contain exactly 49 cases")
    return questions, answers


def _exact_mapping(value: Any, fields: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _digest(value: Any, name: str) -> str:
    digest = _required_text(value, name).lower()
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError(f"{name} must be a prefixed SHA-256 digest")
    try:
        int(digest[7:], 16)
    except ValueError as error:
        raise ValueError(f"{name} must be a prefixed SHA-256 digest") from error
    return digest


def _citation_location(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if any(field not in value for field in CITATION_LOCATION_FIELDS):
        raise ValueError(f"{name} location is incomplete")
    location = {field: value[field] for field in CITATION_LOCATION_FIELDS}
    published_at = location["published_at"]
    if isinstance(published_at, datetime):
        parsed = published_at
    elif isinstance(published_at, str):
        try:
            parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{name} published_at is invalid") from error
    else:
        raise ValueError(f"{name} published_at is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} published_at must be timezone-aware")
    location["published_at"] = parsed.isoformat()
    return location


def build_http_answer_commitment(actual: Mapping[str, Any]) -> dict[str, Any]:
    """Commit one validated HTTP answer without consulting gold or retaining prose."""

    answer = _required_text(actual.get("answer"), "HTTP answer text")
    status = _required_text(actual.get("status"), "HTTP answer status")
    prompt_version = _required_text(
        actual.get("prompt_version"), "HTTP answer prompt version"
    )
    output_schema_version = _required_text(
        actual.get("output_schema_version"), "HTTP answer output schema version"
    )
    failure_code = actual.get("failure_code")
    if failure_code is not None:
        failure_code = _required_text(failure_code, "HTTP answer failure code")
    raw_claims = actual.get("claims")
    raw_citations = actual.get("citations")
    raw_conflicts = actual.get("conflicts", [])
    if not all(
        isinstance(item, (list, tuple))
        for item in (raw_claims, raw_citations, raw_conflicts)
    ):
        raise ValueError("HTTP answer evidence arrays are invalid")

    claims: list[dict[str, Any]] = []
    for index, raw_claim in enumerate(raw_claims):
        if not isinstance(raw_claim, Mapping):
            raise ValueError(f"HTTP answer claim {index} must be an object")
        text = _required_text(raw_claim.get("text"), f"HTTP answer claim {index} text")
        citation_ids = raw_claim.get("citation_ids")
        if (
            not isinstance(citation_ids, (list, tuple))
            or not citation_ids
            or len(set(citation_ids)) != len(citation_ids)
            or any(not isinstance(item, str) or not item for item in citation_ids)
        ):
            raise ValueError(f"HTTP answer claim {index} citations are invalid")
        inference = raw_claim.get("inference")
        material = raw_claim.get("material")
        if not isinstance(inference, bool) or material is not True:
            raise ValueError(f"HTTP answer claim {index} grounding flags are invalid")
        claims.append(
            {
                "citation_ids": list(citation_ids),
                "inference": inference,
                "material": True,
                "text_sha256": _text_digest(text),
            }
        )

    citations: list[dict[str, Any]] = []
    for index, raw_citation in enumerate(raw_citations):
        if not isinstance(raw_citation, Mapping):
            raise ValueError(f"HTTP answer citation {index} must be an object")
        location = _citation_location(
            raw_citation, f"HTTP answer citation {index}"
        )
        citation_id = _required_text(
            raw_citation.get("citation_id"),
            f"HTTP answer citation {index} ID",
        )
        chunk_id = _required_text(
            raw_citation.get("chunk_id"),
            f"HTTP answer citation {index} Chunk ID",
        )
        version_id = _required_text(
            raw_citation.get("version_id"),
            f"HTTP answer citation {index} version ID",
        )
        citations.append(
            {
                "citation_id": citation_id,
                "chunk_id": chunk_id,
                "location_sha256": canonical_quality_digest(location),
                "version_id": version_id,
            }
        )

    conflicts: list[dict[str, Any]] = []
    for index, raw_conflict in enumerate(raw_conflicts):
        if not isinstance(raw_conflict, Mapping):
            raise ValueError(f"HTTP answer conflict {index} must be an object")
        indexes = raw_conflict.get("claim_indexes")
        if not isinstance(indexes, (list, tuple)):
            raise ValueError(f"HTTP answer conflict {index} indexes are invalid")
        conflicts.append(
            {
                "claim_indexes": list(indexes),
                "topic_sha256": _text_digest(
                    _required_text(
                        raw_conflict.get("topic"),
                        f"HTTP answer conflict {index} topic",
                    )
                ),
            }
        )
    return {
        "answer_sha256": _text_digest(answer),
        "citations": citations,
        "claims": claims,
        "conflicts": conflicts,
        "failure_code": failure_code,
        "output_schema_version": output_schema_version,
        "prompt_version": prompt_version,
        "schema_version": HTTP_ANSWER_COMMITMENT_SCHEMA_VERSION,
        "status": status,
    }


def _reconstruct_http_answer(
    item_id: str,
    value: Any,
    gold: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    answer = _exact_mapping(value, _HTTP_ANSWER_FIELDS, f"HTTP answer {item_id}")
    if answer["schema_version"] != HTTP_ANSWER_COMMITMENT_SCHEMA_VERSION:
        raise ValueError(f"HTTP answer {item_id} commitment schema is invalid")
    status = _required_text(answer["status"], f"HTTP answer {item_id} status")
    prompt_version = _required_text(
        answer["prompt_version"], f"HTTP answer {item_id} prompt version"
    )
    output_schema_version = _required_text(
        answer["output_schema_version"],
        f"HTTP answer {item_id} output schema version",
    )
    if prompt_version != PROMPT_VERSION or output_schema_version != OUTPUT_SCHEMA_VERSION:
        raise ValueError(f"HTTP answer {item_id} versions are stale")
    failure_code = answer["failure_code"]
    if failure_code is not None:
        failure_code = _required_text(
            failure_code, f"HTTP answer {item_id} failure code"
        )
    if not all(
        isinstance(answer[field], list)
        for field in ("claims", "citations", "conflicts")
    ):
        raise ValueError(f"HTTP answer {item_id} arrays are invalid")

    gold_claims_by_digest: dict[tuple[bool, str], Mapping[str, Any]] = {}
    for gold_claim in gold.get("claims", []):
        key = (
            bool(gold_claim["inference"]),
            _text_digest(str(gold_claim["reference_text"])),
        )
        if key in gold_claims_by_digest:
            raise ValueError(f"HTTP answer {item_id} gold claim digests are ambiguous")
        gold_claims_by_digest[key] = gold_claim
    claims: list[dict[str, Any]] = []
    normalized_claims: list[dict[str, Any]] = []
    matched_claim_ids: set[str] = set()
    for index, raw_claim in enumerate(answer["claims"]):
        claim = _exact_mapping(
            raw_claim, _HTTP_CLAIM_FIELDS, f"HTTP answer {item_id} claim {index}"
        )
        inference = claim["inference"]
        if not isinstance(inference, bool) or claim["material"] is not True:
            raise ValueError(f"HTTP answer {item_id} claim flags are invalid")
        text_sha256 = _digest(
            claim["text_sha256"], f"HTTP answer {item_id} claim text digest"
        )
        gold_claim = gold_claims_by_digest.get((inference, text_sha256))
        if gold_claim is None or str(gold_claim["claim_id"]) in matched_claim_ids:
            raise ValueError(f"HTTP answer {item_id} claim commitment is not gold")
        matched_claim_ids.add(str(gold_claim["claim_id"]))
        citation_ids = claim["citation_ids"]
        if (
            not isinstance(citation_ids, list)
            or not citation_ids
            or len(set(citation_ids)) != len(citation_ids)
            or any(not isinstance(item, str) or not item for item in citation_ids)
        ):
            raise ValueError(f"HTTP answer {item_id} claim citations are invalid")
        normalized_claims.append(
            {
                "citation_ids": list(citation_ids),
                "inference": inference,
                "material": True,
                "text_sha256": text_sha256,
            }
        )
        claims.append(
            {
                "citation_ids": list(citation_ids),
                "inference": inference,
                "material": True,
                "text": str(gold_claim["reference_text"]),
            }
        )

    evidence_by_chunk = {
        str(source["chunk_id"]): source for source in gold.get("evidence", [])
    }
    citations: list[dict[str, Any]] = []
    normalized_citations: list[dict[str, Any]] = []
    for index, raw_citation in enumerate(answer["citations"]):
        citation = _exact_mapping(
            raw_citation,
            _HTTP_CITATION_FIELDS,
            f"HTTP answer {item_id} citation {index}",
        )
        citation_id = _required_text(
            citation["citation_id"], f"HTTP answer {item_id} citation ID"
        )
        chunk_id = _required_text(
            citation["chunk_id"], f"HTTP answer {item_id} citation Chunk ID"
        )
        version_id = _required_text(
            citation["version_id"], f"HTTP answer {item_id} citation version ID"
        )
        source = evidence_by_chunk.get(chunk_id)
        if source is None or version_id != source.get("version_id"):
            raise ValueError(f"HTTP answer {item_id} citation is outside gold evidence")
        location_sha256 = _digest(
            citation["location_sha256"],
            f"HTTP answer {item_id} citation location digest",
        )
        expected_location = canonical_quality_digest(
            _citation_location(source, f"HTTP answer {item_id} gold citation {index}")
        )
        if location_sha256 != expected_location:
            raise ValueError(f"HTTP answer {item_id} citation location is invalid")
        normalized_citations.append(
            {
                "citation_id": citation_id,
                "chunk_id": chunk_id,
                "location_sha256": location_sha256,
                "version_id": version_id,
            }
        )
        citations.append(
            {
                "citation_id": citation_id,
                **{field: source[field] for field in CITATION_LOCATION_FIELDS},
            }
        )

    if answer["conflicts"]:
        raise ValueError("the fixed HTTP answer sample does not permit conflicts")
    rendered = (
        STANDARD_REFUSAL_ANSWER
        if status == "insufficient_context"
        else "\n".join(
            f"{'Inference: ' if claim['inference'] else ''}{claim['text']} "
            + " ".join(f"[{citation_id}]" for citation_id in claim["citation_ids"])
            for claim in claims
        )
    )
    answer_sha256 = _digest(
        answer["answer_sha256"], f"HTTP answer {item_id} rendered digest"
    )
    if answer_sha256 != _text_digest(rendered):
        raise ValueError(f"HTTP answer {item_id} rendered digest is invalid")
    normalized = {
        "answer_sha256": answer_sha256,
        "citations": normalized_citations,
        "claims": normalized_claims,
        "conflicts": [],
        "failure_code": failure_code,
        "output_schema_version": output_schema_version,
        "prompt_version": prompt_version,
        "schema_version": HTTP_ANSWER_COMMITMENT_SCHEMA_VERSION,
        "status": status,
    }
    reconstructed = {
        "answer": rendered,
        "citations": citations,
        "claims": claims,
        "conflicts": [],
        "failure_code": failure_code,
        "id": item_id,
        "output_schema_version": output_schema_version,
        "prompt_version": prompt_version,
        "status": status,
    }
    return normalized, reconstructed


def evaluate_http_answer_commitments(
    value: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Rebuild committed HTTP answers from pinned gold and run answer metrics."""

    _, answers = _committed_gold()
    gold_by_id = {str(item["id"]): item for item in answers}
    if not value or any(not isinstance(item_id, str) for item_id in value):
        raise ValueError("HTTP answer commitments require case identities")
    normalized: dict[str, dict[str, Any]] = {}
    reconstructed: list[dict[str, Any]] = []
    selected_gold: list[dict[str, Any]] = []
    for item_id in sorted(value):
        gold = gold_by_id.get(item_id)
        if gold is None:
            raise ValueError(f"HTTP answer commitment is not a gold case: {item_id}")
        commitment, actual = _reconstruct_http_answer(item_id, value[item_id], gold)
        normalized[item_id] = commitment
        reconstructed.append(actual)
        selected_gold.append(gold)
    # The HTTP latency workload is a fixed 30-case answered subset of the
    # complete 49-case quality suite.  The lower-level production metric
    # implementation preserves exact pairing/result validation without
    # falsely pretending this subset satisfies the full-dataset coverage
    # contract enforced by ``evaluate_answer_results``.
    metrics = calculate_answer_metrics(selected_gold, reconstructed).as_dict()
    return normalized, metrics


def _answer_projection(
    item_id: str,
    actual: Mapping[str, Any],
    gold: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove prose after proving it is exactly committed synthetic gold text."""
    gold_claims = {claim["claim_id"]: claim for claim in gold["claims"]}
    gold_claims_by_text = {
        (bool(claim["inference"]), str(claim["reference_text"])): claim
        for claim in gold["claims"]
    }
    claims: list[dict[str, Any]] = []
    for index, claim in enumerate(actual["claims"]):
        matched = gold_claims_by_text.get(
            (bool(claim["inference"]), str(claim["text"]))
        )
        if matched is None:
            raise ValueError(
                f"quality answer {item_id} claim {index} is not committed gold text"
            )
        claims.append(
            {
                "citation_ids": list(claim["citation_ids"]),
                "claim_id": matched["claim_id"],
                "inference": claim["inference"],
                "material": claim["material"],
                "text_sha256": _text_digest(str(claim["text"])),
            }
        )
    if len({item["claim_id"] for item in claims}) != len(claims):
        raise ValueError(f"quality answer {item_id} repeats a gold claim")

    evidence_by_chunk = {
        source["chunk_id"]: source for source in gold.get("evidence", [])
    }
    citations: list[dict[str, Any]] = []
    for index, citation in enumerate(actual["citations"]):
        chunk_id = str(citation["chunk_id"])
        source = evidence_by_chunk.get(chunk_id)
        if source is None or any(
            citation.get(field) != source.get(field)
            for field in CITATION_LOCATION_FIELDS
        ):
            raise ValueError(
                f"quality answer {item_id} citation {index} is not committed "
                "gold provenance"
            )
        citations.append(
            {
                "citation_id": citation["citation_id"],
                "chunk_id": chunk_id,
                "location_sha256": canonical_quality_digest(
                    {field: source[field] for field in CITATION_LOCATION_FIELDS}
                ),
            }
        )

    conflicts = [
        {
            "claim_indexes": list(conflict["claim_indexes"]),
            "topic_sha256": _text_digest(str(conflict["topic"])),
        }
        for conflict in actual.get("conflicts", [])
    ]
    # The 49-case gold has no conflict-status answers; rejecting them avoids
    # retaining an unverifiable free-text conflict topic in the evidence.
    if conflicts or gold.get("expected_status") == "conflict":
        raise ValueError("the fixed 49-case quality gold does not permit conflicts")
    if any(item["claim_id"] not in gold_claims for item in claims):
        raise AssertionError("internal gold claim projection is inconsistent")
    return {
        "answer_sha256": _text_digest(str(actual["answer"])),
        "citations": citations,
        "claims": claims,
        "conflicts": conflicts,
        "failure_code": actual.get("failure_code"),
        "output_schema_version": actual.get("output_schema_version"),
        "prompt_version": actual.get("prompt_version"),
        "status": actual.get("status"),
    }


def build_quality_case_evidence(
    actual_retrieval: Sequence[Mapping[str, Any]],
    actual_answers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build canonical 49-case evidence without retaining generated prose."""
    questions, answers = _committed_gold()
    retrieval_rows = [dict(item) for item in actual_retrieval]
    answer_rows = [dict(item) for item in actual_answers]
    # These calls perform the complete runtime-result schema and coverage checks
    # before any free text is discarded.
    evaluate_retrieval_results(questions, retrieval_rows)
    answer_metrics = evaluate_answer_results(answers, answer_rows)
    if (
        answer_metrics.answer_correctness != 1.0
        or answer_metrics.forbidden_answer_exposure_count != 0
        or answer_metrics.generation_failure_count != 0
    ):
        raise ValueError(
            "quality answers must exactly match the committed synthetic gold "
            "before redaction"
        )

    retrieval_by_id = {str(item["id"]): item for item in retrieval_rows}
    actual_answers_by_id = {str(item["id"]): item for item in answer_rows}
    gold_answers_by_id = {str(item["id"]): item for item in answers}
    case_ids = sorted(retrieval_by_id)
    if case_ids != sorted(actual_answers_by_id):
        raise ValueError("quality retrieval and answer case coverage differs")
    cases = []
    for item_id in case_ids:
        retrieval = retrieval_by_id[item_id]
        cases.append(
            {
                "answer": _answer_projection(
                    item_id,
                    actual_answers_by_id[item_id],
                    gold_answers_by_id[item_id],
                ),
                "id": item_id,
                "retrieval": {
                    "ranking": list(retrieval["ranking"]),
                    "visible_resources": [
                        dict(event) for event in retrieval["visible_resources"]
                    ],
                },
            }
        )
    return {
        "answer_gold_sha256": f"sha256:{EXPECTED_ANSWER_SHA256}",
        "case_count": len(cases),
        "case_ids": case_ids,
        "cases": cases,
        "dataset_id": EXPECTED_GOLD_DATASET_ID,
        "dataset_version": EXPECTED_GOLD_VERSION,
        "retrieval_gold_sha256": f"sha256:{EXPECTED_QUESTION_SHA256}",
        "schema_version": QUALITY_CASE_EVIDENCE_SCHEMA_VERSION,
    }


def _reconstruct_answer(
    item_id: str,
    value: Any,
    gold: Mapping[str, Any],
) -> dict[str, Any]:
    answer = _exact_mapping(value, _ANSWER_FIELDS, f"quality case {item_id} answer")
    status = _required_text(answer["status"], f"quality case {item_id} status")
    prompt_version = _required_text(
        answer["prompt_version"], f"quality case {item_id} prompt_version"
    )
    output_schema_version = _required_text(
        answer["output_schema_version"],
        f"quality case {item_id} output_schema_version",
    )
    if (
        prompt_version != PROMPT_VERSION
        or output_schema_version != OUTPUT_SCHEMA_VERSION
    ):
        raise ValueError(f"quality case {item_id} answer versions are stale")
    failure_code = answer["failure_code"]
    if failure_code is not None:
        failure_code = _required_text(
            failure_code, f"quality case {item_id} failure_code"
        )
    raw_claims = answer["claims"]
    raw_citations = answer["citations"]
    raw_conflicts = answer["conflicts"]
    if not all(
        isinstance(item, list)
        for item in (raw_claims, raw_citations, raw_conflicts)
    ):
        raise ValueError(f"quality case {item_id} answer arrays are invalid")

    gold_claims = {str(claim["claim_id"]): claim for claim in gold["claims"]}
    claims: list[dict[str, Any]] = []
    for index, raw_claim in enumerate(raw_claims):
        claim = _exact_mapping(
            raw_claim, _CLAIM_FIELDS, f"quality case {item_id} claim {index}"
        )
        claim_id = _required_text(
            claim["claim_id"], f"quality case {item_id} claim_id"
        )
        gold_claim = gold_claims.get(claim_id)
        if gold_claim is None:
            raise ValueError(f"quality case {item_id} references an unknown gold claim")
        if _digest(
            claim["text_sha256"], f"quality case {item_id} claim text digest"
        ) != _text_digest(str(gold_claim["reference_text"])):
            raise ValueError(f"quality case {item_id} claim text digest is invalid")
        citation_ids = claim["citation_ids"]
        if not isinstance(citation_ids, list):
            raise ValueError(f"quality case {item_id} claim citations are invalid")
        claims.append(
            {
                "citation_ids": list(citation_ids),
                "inference": claim["inference"],
                "material": claim["material"],
                "text": gold_claim["reference_text"],
            }
        )

    evidence_by_chunk = {
        str(source["chunk_id"]): source for source in gold.get("evidence", [])
    }
    citations: list[dict[str, Any]] = []
    for index, raw_citation in enumerate(raw_citations):
        citation = _exact_mapping(
            raw_citation,
            _CITATION_FIELDS,
            f"quality case {item_id} citation {index}",
        )
        chunk_id = _required_text(
            citation["chunk_id"], f"quality case {item_id} citation chunk_id"
        )
        source = evidence_by_chunk.get(chunk_id)
        if source is None:
            raise ValueError(
                f"quality case {item_id} citation is outside gold evidence"
            )
        expected_location = canonical_quality_digest(
            {field: source[field] for field in CITATION_LOCATION_FIELDS}
        )
        if _digest(
            citation["location_sha256"],
            f"quality case {item_id} citation location digest",
        ) != expected_location:
            raise ValueError(
                f"quality case {item_id} citation location digest is invalid"
            )
        citations.append(
            {
                "citation_id": citation["citation_id"],
                **{field: source[field] for field in CITATION_LOCATION_FIELDS},
            }
        )

    conflicts: list[dict[str, Any]] = []
    for index, raw_conflict in enumerate(raw_conflicts):
        conflict = _exact_mapping(
            raw_conflict,
            _CONFLICT_FIELDS,
            f"quality case {item_id} conflict {index}",
        )
        _digest(
            conflict["topic_sha256"], f"quality case {item_id} conflict topic digest"
        )
        if not isinstance(conflict["claim_indexes"], list):
            raise ValueError(f"quality case {item_id} conflict indexes are invalid")
        conflicts.append(
            {
                "claim_indexes": list(conflict["claim_indexes"]),
                "topic": "redacted committed-gold conflict",
            }
        )
    if conflicts:
        raise ValueError("the fixed 49-case quality gold does not permit conflicts")

    if status == "insufficient_context":
        rendered = STANDARD_REFUSAL_ANSWER
    else:
        rendered = "\n".join(
            f"{'Inference: ' if claim['inference'] else ''}{claim['text']} "
            + " ".join(f"[{citation_id}]" for citation_id in claim["citation_ids"])
            for claim in claims
        )
        if status == "conflict":
            rendered = "Conflicting source statements:\n" + rendered
    if _digest(
        answer["answer_sha256"], f"quality case {item_id} answer digest"
    ) != _text_digest(rendered):
        raise ValueError(f"quality case {item_id} answer digest is invalid")
    return {
        "answer": rendered,
        "citations": citations,
        "claims": claims,
        "conflicts": conflicts,
        "failure_code": failure_code,
        "id": item_id,
        "output_schema_version": output_schema_version,
        "prompt_version": prompt_version,
        "status": status,
    }


def evaluate_quality_case_evidence(
    value: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate raw cases and independently recompute every quality metric."""
    evidence = _exact_mapping(value, _TOP_LEVEL_FIELDS, "quality case evidence")
    if evidence["schema_version"] != QUALITY_CASE_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("quality case evidence schema is invalid")
    if (
        evidence["dataset_id"] != EXPECTED_GOLD_DATASET_ID
        or evidence["dataset_version"] != EXPECTED_GOLD_VERSION
    ):
        raise ValueError("quality case evidence gold identity is stale")
    if _digest(
        evidence["retrieval_gold_sha256"], "quality retrieval gold digest"
    ) != f"sha256:{EXPECTED_QUESTION_SHA256}" or _digest(
        evidence["answer_gold_sha256"], "quality answer gold digest"
    ) != f"sha256:{EXPECTED_ANSWER_SHA256}":
        raise ValueError("quality case evidence does not identify committed gold")

    questions, answers = _committed_gold()
    question_ids = sorted(str(item["id"]) for item in questions)
    case_ids = evidence["case_ids"]
    cases = evidence["cases"]
    if (
        isinstance(evidence["case_count"], bool)
        or not isinstance(evidence["case_count"], int)
        or evidence["case_count"] != 49
        or not isinstance(case_ids, list)
        or not isinstance(cases, list)
        or len(case_ids) != 49
        or len(cases) != 49
        or case_ids != question_ids
    ):
        raise ValueError("quality case evidence must cover the exact 49-case gold set")
    gold_answers_by_id = {str(item["id"]): item for item in answers}
    normalized_cases: list[dict[str, Any]] = []
    retrieval_results: list[dict[str, Any]] = []
    answer_results: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    for index, raw_case in enumerate(cases):
        case = _exact_mapping(raw_case, _CASE_FIELDS, f"quality case {index}")
        item_id = _required_text(case["id"], f"quality case {index} id")
        if item_id in observed_ids or item_id not in gold_answers_by_id:
            raise ValueError("quality raw case IDs must be unique committed gold IDs")
        observed_ids.add(item_id)
        retrieval = _exact_mapping(
            case["retrieval"], _RETRIEVAL_FIELDS, f"quality case {item_id} retrieval"
        )
        ranking = retrieval["ranking"]
        visible_resources = retrieval["visible_resources"]
        if not isinstance(ranking, list) or not isinstance(visible_resources, list):
            raise ValueError(f"quality case {item_id} retrieval arrays are invalid")
        normalized_retrieval = {
            "ranking": list(ranking),
            "visible_resources": [dict(item) for item in visible_resources],
        }
        reconstructed_answer = _reconstruct_answer(
            item_id, case["answer"], gold_answers_by_id[item_id]
        )
        normalized_cases.append(
            {
                "answer": dict(case["answer"]),
                "id": item_id,
                "retrieval": normalized_retrieval,
            }
        )
        retrieval_results.append({"id": item_id, **normalized_retrieval})
        answer_results.append(reconstructed_answer)
    if observed_ids != set(question_ids):
        raise ValueError("quality raw case coverage does not match committed gold")
    normalized_cases.sort(key=lambda item: item["id"])
    if [item["id"] for item in normalized_cases] != case_ids:
        raise ValueError("quality raw cases must use canonical case order")

    retrieval_metrics = evaluate_retrieval_results(questions, retrieval_results)
    answer_metrics = evaluate_answer_results(answers, answer_results)
    normalized = {
        "answer_gold_sha256": f"sha256:{EXPECTED_ANSWER_SHA256}",
        "case_count": 49,
        "case_ids": question_ids,
        "cases": normalized_cases,
        "dataset_id": EXPECTED_GOLD_DATASET_ID,
        "dataset_version": EXPECTED_GOLD_VERSION,
        "retrieval_gold_sha256": f"sha256:{EXPECTED_QUESTION_SHA256}",
        "schema_version": QUALITY_CASE_EVIDENCE_SCHEMA_VERSION,
    }
    return normalized, asdict(retrieval_metrics), answer_metrics.as_dict()
