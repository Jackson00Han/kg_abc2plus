#!/usr/bin/env python3
"""Evaluate actual grounded AnswerResult JSONL against versioned answer gold.

The evaluator never generates answers and never reads a recorded prediction
from the gold dataset.  ``--results`` is mandatory and must contain one actual
runtime result for every gold case.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GOLD = ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl"
DEFAULT_CONTRACT = ROOT / "contracts" / "acceptance.v1.json"
QUESTION_CLASSES = frozenset(
    {
        "single_chunk",
        "cross_chunk",
        "graph_relationship",
        "exact_value",
        "temporal_conflict",
        "unanswerable",
        "unauthorized",
    }
)
STATUSES = frozenset({"answered", "insufficient_context", "conflict"})
CITATION_LOCATION_FIELDS = (
    "chunk_id",
    "chunk_checksum",
    "document_id",
    "canonical_uri",
    "source_name",
    "version_id",
    "version_checksum",
    "version_number",
    "ordinal",
    "char_start",
    "char_end",
    "page_number",
    "section",
    "document_title",
    "published_at",
)
_CITATION_ID = re.compile(r"S[1-9][0-9]*\Z")
_INLINE_CITATION = re.compile(r"\[(S[1-9][0-9]*)\]")
STANDARD_REFUSAL_ANSWER = "I don't have enough cited context to answer this question."
EXPECTED_CORPUS_ID = "dev-corpus-v1"
EXPECTED_CORPUS_VERSION = "1.0.1"
EXPECTED_GOLD_VERSION = "1.1.0"
MAX_CITATIONS_PER_CLAIM = 5
MAX_CLAIMS = 20
_ANSWER_QUANTITY = re.compile(
    r"^(?P<currency>\$)?(?P<number>\d+(?:\.\d+)?)"
    r"(?:\s+(?P<scale>billion))?(?P<percent>%)?$"
)


@dataclass(frozen=True, slots=True)
class GroundedAnswerMetrics:
    item_count: int
    material_claim_count: int
    citation_attachment_count: int
    exact_token_count: int
    expected_refusal_count: int
    expected_conflict_count: int
    expected_temporal_comparison_count: int
    generation_failure_count: int
    forbidden_answer_exposure_count: int
    supported_claim_rate: float
    citation_precision: float
    citation_coverage: float
    numerical_fidelity: float
    refusal_precision: float
    refusal_recall: float
    refusal_f1: float
    answer_correctness: float
    conflict_handling_rate: float | None
    temporal_comparison_rate: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain a JSON object")
            records.append(value)
    return records


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _is_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _answer_quantity_parts(value: str) -> tuple[Decimal, str] | None:
    match = _ANSWER_QUANTITY.fullmatch(value)
    if match is None:
        return None
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation:
        return None
    qualifier = "".join(
        (
            match.group("currency") or "",
            f" {match.group('scale')}" if match.group("scale") else "",
            match.group("percent") or "",
        )
    )
    return number, qualifier


def _unique_items(items: Sequence[Mapping[str, Any]], name: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"{name}[{index}] must be an object")
        item_id = _required_text(item.get("id"), f"{name}[{index}].id")
        if item_id in result:
            raise ValueError(f"{name} IDs must be unique: {item_id}")
        result[item_id] = item
    return result


def validate_gold_dataset(items: Sequence[Mapping[str, Any]]) -> None:
    """Fail closed unless the complete seven-class, 49-case gold is present."""
    by_id = _unique_items(items, "gold")
    if len(by_id) != 49:
        raise ValueError("answer gold must contain exactly 49 cases")
    quotas: Counter[tuple[str, str]] = Counter()
    statuses: set[str] = set()
    corpus_identity: set[tuple[str, str, str]] = set()
    for item_id, item in by_id.items():
        recorded_result_fields = {
            "actual_result",
            "predicted_answer",
            "prediction",
        } & set(item)
        if recorded_result_fields:
            raise ValueError(
                f"gold {item_id} must not contain recorded runtime results"
            )
        question_class = item.get("question_class")
        case_type = item.get("case_type")
        if question_class not in QUESTION_CLASSES:
            raise ValueError(f"gold {item_id} has an unknown question class")
        if case_type not in {"success", "boundary"}:
            raise ValueError(f"gold {item_id} has an invalid case type")
        quotas[(str(question_class), str(case_type))] += 1
        status = item.get("expected_status")
        if status not in STATUSES:
            raise ValueError(f"gold {item_id} has an invalid expected status")
        refusal_class = question_class in {"unanswerable", "unauthorized"}
        if refusal_class != (status == "insufficient_context"):
            raise ValueError(f"gold {item_id} has inconsistent refusal behavior")
        statuses.add(str(status))
        identity = (
            _required_text(item.get("corpus_id"), f"gold {item_id}.corpus_id"),
            _required_text(item.get("corpus_version"), f"gold {item_id}.corpus_version"),
            _required_text(item.get("gold_version"), f"gold {item_id}.gold_version"),
        )
        corpus_identity.add(identity)
        if identity != (
            EXPECTED_CORPUS_ID,
            EXPECTED_CORPUS_VERSION,
            EXPECTED_GOLD_VERSION,
        ):
            raise ValueError(f"gold {item_id} has a stale or unknown dataset version")
        claims = item.get("claims")
        evidence = item.get("evidence")
        if not isinstance(claims, list) or not isinstance(evidence, list):
            raise ValueError(f"gold {item_id} claims and evidence must be lists")
        if item.get("expected_material_claim_count") != len(claims):
            raise ValueError(f"gold {item_id} material claim count is inconsistent")
        if status == "insufficient_context":
            if claims or evidence:
                raise ValueError(
                    f"refusal gold {item_id} cannot contain factual claims or evidence"
                )
        elif not claims or not evidence:
            raise ValueError(f"answer gold {item_id} requires claims and evidence")
        evidence_ids: set[str] = set()
        evidence_provenance: set[tuple[str, str]] = set()
        for index, source in enumerate(evidence):
            if not isinstance(source, Mapping):
                raise ValueError(f"gold {item_id} evidence {index} must be an object")
            for field in CITATION_LOCATION_FIELDS:
                if field not in source:
                    raise ValueError(
                        f"gold {item_id} evidence {index} is missing {field}"
                    )
            chunk_id = _required_text(
                source.get("chunk_id"), f"gold {item_id} evidence {index}.chunk_id"
            )
            if chunk_id in evidence_ids:
                raise ValueError(f"gold {item_id} repeats evidence {chunk_id}")
            evidence_ids.add(chunk_id)
            evidence_provenance.add(
                (
                    _required_text(
                        source.get("document_id"),
                        f"gold {item_id} evidence {index}.document_id",
                    ),
                    _required_text(
                        source.get("version_id"),
                        f"gold {item_id} evidence {index}.version_id",
                    ),
                )
            )
        claim_ids: set[str] = set()
        canonical_claims: set[tuple[bool, str]] = set()
        used_evidence_ids: set[str] = set()
        exact_tokens: list[str] = []
        reference_answer = _required_text(
            item.get("reference_answer"), f"gold {item_id}.reference_answer"
        )
        for index, claim in enumerate(claims):
            if not isinstance(claim, Mapping):
                raise ValueError(f"gold {item_id} claim {index} must be an object")
            claim_id = _required_text(
                claim.get("claim_id"), f"gold {item_id} claim {index}.claim_id"
            )
            if claim_id in claim_ids:
                raise ValueError(f"gold {item_id} repeats claim {claim_id}")
            claim_ids.add(claim_id)
            if claim.get("material") is not True:
                raise ValueError(f"gold {item_id} claims must be material")
            if not isinstance(claim.get("inference"), bool):
                raise ValueError(f"gold {item_id} claim inference must be boolean")
            text = _required_text(
                claim.get("reference_text"),
                f"gold {item_id} claim {index}.reference_text",
            )
            if text.startswith("Inference:"):
                raise ValueError(
                    f"gold {item_id} inference labels belong in rendered answers"
                )
            canonical_claim = (bool(claim["inference"]), text)
            if canonical_claim in canonical_claims:
                raise ValueError(
                    f"gold {item_id} repeats a canonical adjudicated claim"
                )
            canonical_claims.add(canonical_claim)
            rendered = f"Inference: {text}" if claim["inference"] else text
            if rendered not in reference_answer:
                raise ValueError(
                    f"gold {item_id} reference answer omits a rendered claim"
                )
            required_terms = claim.get("required_terms")
            claim_evidence = claim.get("evidence_chunk_ids")
            claim_tokens = claim.get("exact_tokens")
            if (
                not isinstance(required_terms, list)
                or not required_terms
                or any(not isinstance(term, str) or not term for term in required_terms)
            ):
                raise ValueError(f"gold {item_id} claim required terms are invalid")
            if (
                not isinstance(claim_evidence, list)
                or not claim_evidence
                or not set(claim_evidence) <= evidence_ids
            ):
                raise ValueError(f"gold {item_id} claim evidence is invalid")
            used_evidence_ids.update(str(value) for value in claim_evidence)
            if not isinstance(claim_tokens, list) or any(
                not isinstance(token, str) or not token for token in claim_tokens
            ):
                raise ValueError(f"gold {item_id} claim exact tokens are invalid")
            if any(term.casefold() not in text.casefold() for term in required_terms):
                raise ValueError(f"gold {item_id} claim omits a required term")
            if any(token not in text for token in claim_tokens):
                raise ValueError(f"gold {item_id} claim omits an exact token")
            exact_tokens.extend(claim_tokens)
            comparison = claim.get("comparison")
            if claim["inference"]:
                fields = {
                    "direction",
                    "from_period",
                    "from_value",
                    "to_period",
                    "to_value",
                }
                if not isinstance(comparison, Mapping) or set(comparison) != fields:
                    raise ValueError(
                        f"gold {item_id} inference lacks an auditable comparison"
                    )
                from_parts = _answer_quantity_parts(str(comparison["from_value"]))
                to_parts = _answer_quantity_parts(str(comparison["to_value"]))
                if (
                    from_parts is None
                    or to_parts is None
                    or from_parts[1] != to_parts[1]
                ):
                    raise ValueError(
                        f"gold {item_id} comparison quantities are incompatible"
                    )
                actual_direction = (
                    "increased"
                    if to_parts[0] > from_parts[0]
                    else "decreased"
                    if to_parts[0] < from_parts[0]
                    else "unchanged"
                )
                if comparison["direction"] != actual_direction:
                    raise ValueError(
                        f"gold {item_id} comparison direction disagrees with operands"
                    )
                for field in ("direction", "from_period", "to_period"):
                    if str(comparison[field]) not in text:
                        raise ValueError(
                            f"gold {item_id} comparison {field} is absent from its claim"
                        )
            elif comparison is not None:
                raise ValueError(
                    f"gold {item_id} sourced claim cannot declare a comparison"
                )
        if used_evidence_ids != evidence_ids:
            raise ValueError(
                f"gold {item_id} top-level evidence must exactly equal claim evidence"
            )
        indexed_exact_tokens = list(dict.fromkeys(exact_tokens))
        if item.get("required_exact_tokens") != indexed_exact_tokens:
            raise ValueError(f"gold {item_id} exact-token index is inconsistent")
        forbidden_terms = item.get("forbidden_answer_terms")
        if not isinstance(forbidden_terms, list):
            raise ValueError(f"gold {item_id} forbidden answer terms must be a list")
        if question_class == "unauthorized" and not forbidden_terms:
            raise ValueError(f"unauthorized gold {item_id} requires protected terms")
        if question_class == "exact_value" and not indexed_exact_tokens:
            raise ValueError(f"exact-value gold {item_id} requires exact tokens")
        if status == "conflict":
            conflict = item.get("conflict")
            if not isinstance(conflict, Mapping) or conflict.get("required") is not True:
                raise ValueError(f"conflict gold {item_id} requires a conflict contract")
            if len(claims) < 2 or len(evidence_provenance) < 2:
                raise ValueError(
                    f"conflict gold {item_id} requires alternatives from distinct sources"
                )
        elif item.get("conflict") is not None:
            raise ValueError(f"non-conflict gold {item_id} declares a conflict")
        temporal = item.get("temporal_comparison")
        if temporal is not None:
            if status != "answered" or not isinstance(temporal, Mapping):
                raise ValueError(f"temporal gold {item_id} must be answered")
            periods = temporal.get("required_periods")
            if (
                temporal.get("required") is not True
                or temporal.get("must_label_inference") is not True
                or not isinstance(periods, list)
                or len(periods) < 2
                or any(not isinstance(period, str) or not period for period in periods)
                or not isinstance(temporal.get("inference_direction"), str)
                or not any(claim.get("inference") for claim in claims)
            ):
                raise ValueError(f"temporal gold {item_id} has an invalid contract")
            if any(period not in reference_answer for period in periods):
                raise ValueError(f"temporal gold {item_id} omits a required period")
            direction = str(temporal["inference_direction"])
            if not any(
                claim.get("inference")
                and _contains(str(claim["reference_text"]), direction)
                and isinstance(claim.get("comparison"), Mapping)
                and claim["comparison"].get("direction") == direction
                for claim in claims
            ):
                raise ValueError(f"temporal gold {item_id} omits its inference direction")
    for question_class in QUESTION_CLASSES:
        if quotas[(question_class, "success")] != 5:
            raise ValueError(f"{question_class} requires five success cases")
        if quotas[(question_class, "boundary")] != 2:
            raise ValueError(f"{question_class} requires two boundary cases")
    if len(corpus_identity) != 1:
        raise ValueError("answer gold corpus/gold versions must be uniform")
    if not {"answered", "insufficient_context"} <= statuses:
        raise ValueError("answer gold must cover answered and refused behavior")


def _validate_actual_result(item_id: str, result: Mapping[str, Any]) -> None:
    if result.get("prompt_version") != PROMPT_VERSION:
        raise ValueError(f"result {item_id} has an unknown prompt version")
    if result.get("output_schema_version") != OUTPUT_SCHEMA_VERSION:
        raise ValueError(f"result {item_id} has an unknown output schema version")
    status = result.get("status")
    if status not in STATUSES:
        raise ValueError(f"result {item_id} has an invalid status")
    answer = _required_text(result.get("answer"), f"result {item_id}.answer")
    claims = result.get("claims")
    citations = result.get("citations")
    conflicts = result.get("conflicts", [])
    if not _is_array(claims) or not _is_array(citations) or not _is_array(conflicts):
        raise ValueError(
            f"result {item_id} claims, citations, and conflicts must be lists"
        )
    if len(claims) > MAX_CLAIMS:
        raise ValueError(f"result {item_id} exceeds the claim limit")
    failure_code = result.get("failure_code")
    if failure_code is not None:
        _required_text(failure_code, f"result {item_id}.failure_code")
        if status != "insufficient_context":
            raise ValueError(
                f"result {item_id} only permits failure_code on insufficient context"
            )
    if status == "insufficient_context":
        if claims or citations or conflicts:
            raise ValueError(
                f"result {item_id} insufficient context cannot contain evidence"
            )
        if answer != STANDARD_REFUSAL_ANSWER:
            raise ValueError(f"result {item_id} must use the standard refusal")
        return
    if failure_code is not None:
        raise ValueError(f"result {item_id} answered results cannot have failure_code")
    if not claims or not citations:
        raise ValueError(f"result {item_id} answered/conflict requires claims and citations")
    if status == "answered" and conflicts:
        raise ValueError(f"result {item_id} answered status forbids conflicts")
    if status == "conflict" and not conflicts:
        raise ValueError(f"result {item_id} conflict status requires conflict details")
    citation_ids: set[str] = set()
    citation_chunk_ids: set[str] = set()
    citation_provenance: set[tuple[Any, ...]] = set()
    for index, citation in enumerate(citations):
        if not isinstance(citation, Mapping):
            raise ValueError(f"result {item_id} citation {index} must be an object")
        citation_id = _required_text(
            citation.get("citation_id"),
            f"result {item_id} citation {index}.citation_id",
        )
        if _CITATION_ID.fullmatch(citation_id) is None:
            raise ValueError(
                f"result {item_id} citation IDs must use the S<number> format"
            )
        if citation_id in citation_ids:
            raise ValueError(f"result {item_id} citation IDs must be unique")
        citation_ids.add(citation_id)
        for field in CITATION_LOCATION_FIELDS:
            if field not in citation:
                raise ValueError(f"result {item_id} citation {citation_id} is missing {field}")
        chunk_id = str(citation["chunk_id"])
        provenance = tuple(
            json.dumps(citation[field], sort_keys=True, default=str)
            for field in CITATION_LOCATION_FIELDS
        )
        if chunk_id in citation_chunk_ids or provenance in citation_provenance:
            raise ValueError(f"result {item_id} repeats citation provenance")
        citation_chunk_ids.add(str(chunk_id))
        citation_provenance.add(provenance)
    referenced_citation_ids: set[str] = set()
    canonical_claims: set[tuple[bool, str]] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            raise ValueError(f"result {item_id} claim {index} must be an object")
        _required_text(claim.get("text"), f"result {item_id} claim {index}.text")
        if claim.get("material") is not True:
            raise ValueError(f"result {item_id} claim {index}.material must be true")
        if not isinstance(claim.get("inference"), bool):
            raise ValueError(f"result {item_id} claim {index}.inference must be boolean")
        canonical_claim = (bool(claim["inference"]), str(claim["text"]))
        if canonical_claim in canonical_claims:
            raise ValueError(f"result {item_id} repeats a canonical claim")
        canonical_claims.add(canonical_claim)
        attached = claim.get("citation_ids")
        if not _is_array(attached) or not attached or any(
            not isinstance(value, str) or not value for value in attached
        ):
            raise ValueError(
                f"result {item_id} claim {index}.citation_ids must be non-empty text IDs"
            )
        if len(attached) != len(set(attached)):
            raise ValueError(f"result {item_id} claim {index} repeats a citation")
        if len(attached) > MAX_CITATIONS_PER_CLAIM:
            raise ValueError(f"result {item_id} claim {index} exceeds citation limit")
        unknown = set(attached) - citation_ids
        if unknown:
            raise ValueError(f"result {item_id} claim {index} references unknown citations")
        referenced_citation_ids.update(attached)
        claim_text = str(claim["text"])
        if claim_text not in answer:
            raise ValueError(f"result {item_id} answer omits claim {index}")
        if claim["inference"] and f"Inference: {claim_text}" not in answer:
            raise ValueError(f"result {item_id} does not label inference claim {index}")
        if any(f"[{citation_id}]" not in answer for citation_id in attached):
            raise ValueError(f"result {item_id} omits an inline claim citation")
    if referenced_citation_ids != citation_ids:
        raise ValueError(f"result {item_id} citations must all be referenced")
    if set(_INLINE_CITATION.findall(answer)) != citation_ids:
        raise ValueError(f"result {item_id} has unknown or missing inline citations")
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, Mapping):
            raise ValueError(f"result {item_id} conflict {index} must be an object")
        _required_text(
            conflict.get("topic"), f"result {item_id} conflict {index}.topic"
        )
        claim_indexes = conflict.get("claim_indexes")
        if (
            not _is_array(claim_indexes)
            or len(claim_indexes) < 2
            or len(claim_indexes) != len(set(claim_indexes))
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value >= len(claims)
                for value in claim_indexes
            )
        ):
            raise ValueError(f"result {item_id} conflict claim indexes are invalid")
    if status == "conflict":
        if any(claim["inference"] for claim in claims):
            raise ValueError(f"result {item_id} conflict alternatives must be sourced")
        grouped_indexes = [
            value
            for conflict in conflicts
            for value in conflict["claim_indexes"]
        ]
        if sorted(grouped_indexes) != list(range(len(claims))):
            raise ValueError(
                f"result {item_id} conflict groups must cover every claim exactly once"
            )
        citations_by_id = {
            citation["citation_id"]: citation for citation in citations
        }
        for conflict in conflicts:
            provenance_sets = [
                frozenset(
                    (
                        str(citations_by_id[citation_id]["document_id"]),
                        str(citations_by_id[citation_id]["version_id"]),
                    )
                    for citation_id in claims[index]["citation_ids"]
                )
                for index in conflict["claim_indexes"]
            ]
            if (
                len(set(provenance_sets)) < 2
                or len(set().union(*provenance_sets)) < 2
            ):
                raise ValueError(
                    f"result {item_id} conflict alternatives require distinct provenance"
                )
    rendered = "\n".join(
        f"{'Inference: ' if claim['inference'] else ''}{claim['text']} "
        + " ".join(f"[{citation_id}]" for citation_id in claim["citation_ids"])
        for claim in claims
    )
    if status == "conflict":
        rendered = "Conflicting source statements:\n" + rendered
    if answer != rendered:
        raise ValueError(f"result {item_id} answer is not the canonical server rendering")


def _contains(text: str, term: str) -> bool:
    return term.casefold() in text.casefold()


def _matches_gold_claim(actual_claim: Mapping[str, Any], gold_claim: Mapping[str, Any]) -> bool:
    if bool(actual_claim["inference"]) != bool(gold_claim["inference"]):
        return False
    # A bag of required terms cannot distinguish a supported statement from a
    # subject/object reversal or an inference with the opposite direction.
    # Gold claims are adjudicated, versioned accepted statements, so the
    # regression metric deliberately requires the complete claim text.
    return str(actual_claim["text"]) == str(gold_claim["reference_text"])


def _citation_location_matches(
    citation: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> bool:
    return all(citation.get(field) == evidence.get(field) for field in CITATION_LOCATION_FIELDS)


def calculate_answer_metrics(
    gold_items: Sequence[Mapping[str, Any]],
    actual_items: Sequence[Mapping[str, Any]],
) -> GroundedAnswerMetrics:
    """Calculate metrics after exact result pairing; callers validate gold scope."""
    gold_by_id = _unique_items(gold_items, "gold")
    actual_by_id = _unique_items(actual_items, "results")
    if set(gold_by_id) != set(actual_by_id):
        missing = sorted(set(gold_by_id) - set(actual_by_id))
        extra = sorted(set(actual_by_id) - set(gold_by_id))
        raise ValueError(f"actual result coverage mismatch: missing={missing}, extra={extra}")
    for item_id, result in actual_by_id.items():
        _validate_actual_result(item_id, result)

    material_total = 0
    supported_total = 0
    covered_total = 0
    citation_total = 0
    supported_citations = 0
    exact_total = 0
    exact_correct = 0
    correct_cases = 0
    expected_refusals = 0
    predicted_refusals = 0
    true_refusals = 0
    expected_conflicts = 0
    handled_conflicts = 0
    expected_temporal_comparisons = 0
    handled_temporal_comparisons = 0
    generation_failures = 0
    forbidden_answer_exposures = 0

    for item_id, gold in gold_by_id.items():
        actual = actual_by_id[item_id]
        answer_text = str(actual["answer"])
        actual_claims = list(actual["claims"])
        citations_by_id = {
            citation["citation_id"]: citation for citation in actual["citations"]
        }
        evidence_by_chunk = {
            source["chunk_id"]: source for source in gold.get("evidence", [])
        }
        claim_matches: list[list[Mapping[str, Any]]] = [
            [
                gold_claim
                for gold_claim in gold["claims"]
                if _matches_gold_claim(actual_claim, gold_claim)
            ]
            for actual_claim in actual_claims
        ]

        material_total += len(actual_claims)
        actual_claim_supported: list[bool] = []
        actual_claim_covered: list[bool] = []
        for actual_claim, matches in zip(actual_claims, claim_matches):
            claim_covered = False
            citation_support: list[bool] = []
            for citation_id in actual_claim["citation_ids"]:
                citation_total += 1
                citation = citations_by_id[citation_id]
                evidence = evidence_by_chunk.get(citation["chunk_id"])
                location_valid = evidence is not None and _citation_location_matches(
                    citation, evidence
                )
                inline = f"[{citation_id}]" in answer_text
                entails = any(
                    citation["chunk_id"] in match["evidence_chunk_ids"]
                    for match in matches
                )
                supported = bool(location_valid and inline and entails)
                citation_support.append(supported)
                if supported:
                    claim_covered = True
                    supported_citations += 1
            actual_claim_supported.append(
                bool(matches) and bool(citation_support) and all(citation_support)
            )
            actual_claim_covered.append(claim_covered)
        supported_total += sum(actual_claim_supported)
        covered_total += sum(actual_claim_covered)

        gold_claim_supported: list[bool] = []
        for gold_claim in gold["claims"]:
            supported = False
            for actual_claim in actual_claims:
                if not _matches_gold_claim(actual_claim, gold_claim):
                    continue
                for citation_id in actual_claim["citation_ids"]:
                    citation = citations_by_id[citation_id]
                    evidence = evidence_by_chunk.get(citation["chunk_id"])
                    if (
                        citation["chunk_id"] in gold_claim["evidence_chunk_ids"]
                        and evidence is not None
                        and _citation_location_matches(citation, evidence)
                        and f"[{citation_id}]" in answer_text
                    ):
                        supported = True
                        break
                if supported:
                    break
            gold_claim_supported.append(supported)

            for token in gold_claim["exact_tokens"]:
                exact_total += 1
                token_preserved = token in answer_text and any(
                    token in str(actual_claim["text"])
                    and _matches_gold_claim(actual_claim, gold_claim)
                    and any(
                        (
                            citations_by_id[citation_id]["chunk_id"]
                            in gold_claim["evidence_chunk_ids"]
                            and (
                                source := evidence_by_chunk.get(
                                    citations_by_id[citation_id]["chunk_id"]
                                )
                            )
                            is not None
                            and _citation_location_matches(
                                citations_by_id[citation_id], source
                            )
                            and f"[{citation_id}]" in answer_text
                        )
                        for citation_id in actual_claim["citation_ids"]
                    )
                    for actual_claim in actual_claims
                )
                exact_correct += int(token_preserved)

        expected_refusal = gold["expected_status"] == "insufficient_context"
        generation_failure = actual.get("failure_code") is not None
        predicted_refusal = (
            actual["status"] == "insufficient_context" and not generation_failure
        )
        expected_refusals += int(expected_refusal)
        predicted_refusals += int(predicted_refusal)
        true_refusals += int(expected_refusal and predicted_refusal)
        generation_failures += int(generation_failure)

        forbidden_hits = sum(
            _contains(answer_text, str(term))
            for term in gold.get("forbidden_answer_terms", [])
        )
        forbidden_answer_exposures += forbidden_hits
        forbidden_leak = forbidden_hits > 0
        status_correct = actual["status"] == gold["expected_status"]
        if expected_refusal:
            case_correct = predicted_refusal and not actual_claims and not forbidden_leak
        else:
            case_correct = (
                status_correct
                and not generation_failure
                and all(gold_claim_supported)
                and all(actual_claim_supported)
                and not forbidden_leak
            )

        if gold["expected_status"] == "conflict":
            expected_conflicts += 1
            provenance_safe_conflict = False
            for conflict in actual.get("conflicts", []):
                provenance_sets: list[frozenset[tuple[str, str]]] = []
                indexes = conflict["claim_indexes"]
                if not all(actual_claim_supported[index] for index in indexes):
                    continue
                for index in indexes:
                    provenance_sets.append(
                        frozenset(
                            (
                                str(citations_by_id[citation_id]["document_id"]),
                                str(citations_by_id[citation_id]["version_id"]),
                            )
                            for citation_id in actual_claims[index]["citation_ids"]
                        )
                    )
                if (
                    len(set(provenance_sets)) >= 2
                    and len(set().union(*provenance_sets)) >= 2
                ):
                    provenance_safe_conflict = True
                    break
            handled = (
                status_correct
                and all(gold_claim_supported)
                and provenance_safe_conflict
            )
            handled_conflicts += int(handled)
            case_correct = case_correct and handled

        temporal = gold.get("temporal_comparison")
        if temporal is not None:
            expected_temporal_comparisons += 1
            periods_present = all(
                _contains(answer_text, str(period))
                for period in temporal["required_periods"]
            )
            labelled_inference = any(
                claim["inference"]
                and _contains(
                    str(claim["text"]), str(temporal["inference_direction"])
                )
                and f"Inference: {claim['text']}" in answer_text
                for claim in actual_claims
            )
            handled = (
                actual["status"] == "answered"
                and periods_present
                and labelled_inference
                and all(gold_claim_supported)
            )
            handled_temporal_comparisons += int(handled)
            case_correct = case_correct and handled
        correct_cases += int(case_correct)

    false_refusals = predicted_refusals - true_refusals
    missed_refusals = expected_refusals - true_refusals
    refusal_precision = (
        true_refusals / predicted_refusals if predicted_refusals else 0.0
    )
    refusal_recall = true_refusals / expected_refusals if expected_refusals else 0.0
    refusal_f1 = (
        0.0
        if 2 * true_refusals + false_refusals + missed_refusals == 0
        else 2
        * true_refusals
        / (2 * true_refusals + false_refusals + missed_refusals)
    )
    return GroundedAnswerMetrics(
        item_count=len(gold_by_id),
        material_claim_count=material_total,
        citation_attachment_count=citation_total,
        exact_token_count=exact_total,
        expected_refusal_count=expected_refusals,
        expected_conflict_count=expected_conflicts,
        expected_temporal_comparison_count=expected_temporal_comparisons,
        generation_failure_count=generation_failures,
        forbidden_answer_exposure_count=forbidden_answer_exposures,
        supported_claim_rate=(supported_total / material_total if material_total else 0.0),
        citation_precision=(supported_citations / citation_total if citation_total else 0.0),
        citation_coverage=(covered_total / material_total if material_total else 0.0),
        numerical_fidelity=(exact_correct / exact_total if exact_total else 0.0),
        refusal_precision=refusal_precision,
        refusal_recall=refusal_recall,
        refusal_f1=refusal_f1,
        answer_correctness=correct_cases / len(gold_by_id),
        conflict_handling_rate=(
            handled_conflicts / expected_conflicts if expected_conflicts else None
        ),
        temporal_comparison_rate=(
            handled_temporal_comparisons / expected_temporal_comparisons
            if expected_temporal_comparisons
            else None
        ),
    )


def evaluate_answer_results(
    gold_items: Sequence[Mapping[str, Any]],
    actual_items: Sequence[Mapping[str, Any]],
) -> GroundedAnswerMetrics:
    validate_gold_dataset(gold_items)
    return calculate_answer_metrics(gold_items, actual_items)


def answer_gate_failures(
    metrics: GroundedAnswerMetrics,
    contract: Mapping[str, Any],
) -> tuple[str, ...]:
    """Apply Stage 1 targets plus complete-case deterministic regression gates."""
    targets = {item["id"]: item for item in contract["metrics"]}
    observed = {
        "supported_claim_rate": metrics.supported_claim_rate,
        "citation_precision": metrics.citation_precision,
        "citation_coverage": metrics.citation_coverage,
        "numerical_fidelity": metrics.numerical_fidelity,
        "refusal_f1": metrics.refusal_f1,
    }
    failures: list[str] = []
    for metric_id, value in observed.items():
        metric = targets[metric_id]
        target = metric["target"]
        passed = {
            ">=": value >= target,
            "<=": value <= target,
            "=": value == target,
        }[metric["operator"]]
        if not passed:
            failures.append(
                f"{metric_id}: {value} {metric['operator']} {target} failed"
            )

    # Rate denominators based only on returned claims can remain perfect when
    # a system silently drops one complete answer. The fixed gold regression
    # therefore also requires every case to be correct and no dependency or
    # validation failure to masquerade as a refusal.
    if metrics.answer_correctness != 1.0:
        failures.append(
            f"answer_correctness: {metrics.answer_correctness} = 1.0 failed"
        )
    if metrics.generation_failure_count != 0:
        failures.append(
            "generation_failure_count: "
            f"{metrics.generation_failure_count} = 0 failed"
        )
    if metrics.forbidden_answer_exposure_count != 0:
        failures.append(
            "forbidden_answer_exposure_count: "
            f"{metrics.forbidden_answer_exposure_count} = 0 failed"
        )
    if (
        metrics.temporal_comparison_rate is not None
        and metrics.temporal_comparison_rate != 1.0
    ):
        failures.append(
            "temporal_comparison_rate: "
            f"{metrics.temporal_comparison_rate} = 1.0 failed"
        )
    if (
        metrics.conflict_handling_rate is not None
        and metrics.conflict_handling_rate != 1.0
    ):
        failures.append(
            "conflict_handling_rate: "
            f"{metrics.conflict_handling_rate} = 1.0 failed"
        )
    return tuple(failures)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="JSONL of actual runtime AnswerResult records; no default is allowed",
    )
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gold = load_jsonl(args.gold)
    actual = load_jsonl(args.results)
    metrics = evaluate_answer_results(gold, actual)
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    failures = answer_gate_failures(metrics, contract)
    for key, value in metrics.as_dict().items():
        if isinstance(value, float):
            print(f"{key}={value:.4f}")
        else:
            print(f"{key}={value}")
    if failures:
        raise SystemExit("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
