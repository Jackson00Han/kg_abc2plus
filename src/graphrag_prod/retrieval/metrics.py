"""Retrieval regression metrics with hand-computable definitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from numbers import Real
from pathlib import Path
from typing import Any, Mapping, Sequence


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


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    item_count: int
    answerable_count: int
    recall_at_5: float
    evidence_recall_at_5: float
    mrr: float
    ndcg_at_5: float
    unauthorized_exposure_count: int


def _positive_relevant(item: dict[str, Any]) -> set[str]:
    return {
        str(chunk_id)
        for chunk_id, grade in dict(item["relevance"]).items()
        if float(grade) > 0.0
    }


def _reciprocal_rank(ranking: Sequence[str], relevant: set[str]) -> float:
    for rank, chunk_id in enumerate(ranking, start=1):
        if chunk_id in relevant:
            return 1.0 / rank
    return 0.0


def _dcg(grades: Sequence[float]) -> float:
    return math.fsum(
        (2.0**grade - 1.0) / math.log2(rank + 1)
        for rank, grade in enumerate(grades, start=1)
    )


def _ndcg_at_k(item: dict[str, Any], k: int) -> float:
    relevance = {str(key): float(value) for key, value in item["relevance"].items()}
    actual = [relevance.get(str(chunk_id), 0.0) for chunk_id in item["ranking"][:k]]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    ideal_dcg = _dcg(ideal)
    return 0.0 if ideal_dcg == 0.0 else _dcg(actual) / ideal_dcg


def evaluate_retrieval_items(items: Sequence[dict[str, Any]]) -> RetrievalMetrics:
    """Evaluate already-paired rows using standard fractional Recall@5.

    Stage 8 callers should use :func:`evaluate_retrieval_results`, which
    additionally applies the acceptance contract's per-fact evidence groups.
    This lower-level helper remains useful for historical fixtures that only
    carry graded relevance judgments.
    """
    if not items:
        raise ValueError("retrieval dataset must not be empty")
    answerable = [item for item in items if item["answerable"]]
    if not answerable:
        raise ValueError("retrieval dataset must contain answerable items")
    recalls: list[float] = []
    evidence_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for item in answerable:
        relevant = _positive_relevant(item)
        if not relevant:
            raise ValueError(f"answerable item {item['id']} has no relevant chunks")
        ranking = [str(value) for value in item["ranking"]]
        retrieved = set(ranking[:5])
        evidence_recall = len(relevant & retrieved) / len(relevant)
        recalls.append(evidence_recall)
        evidence_recalls.append(evidence_recall)
        reciprocal_ranks.append(_reciprocal_rank(ranking, relevant))
        ndcgs.append(_ndcg_at_k(item, 5))
    exposures = sum(len(item["unauthorized_exposures"]) for item in items)
    return RetrievalMetrics(
        item_count=len(items),
        answerable_count=len(answerable),
        recall_at_5=math.fsum(recalls) / len(recalls),
        evidence_recall_at_5=math.fsum(evidence_recalls) / len(evidence_recalls),
        mrr=math.fsum(reciprocal_ranks) / len(reciprocal_ranks),
        ndcg_at_5=math.fsum(ndcgs) / len(ndcgs),
        unauthorized_exposure_count=exposures,
    )


def evaluate_retrieval_results(
    gold_items: Sequence[dict[str, Any]],
    actual_items: Sequence[dict[str, Any]],
) -> RetrievalMetrics:
    """Pair independent gold annotations with actual runtime rankings."""

    def by_id(items: Sequence[dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{name}[{index}] must be an object")
            item_id = str(item.get("id", "")).strip()
            if not item_id or item_id in result:
                raise ValueError(f"{name} IDs must be unique and non-empty")
            result[item_id] = item
        return result

    gold_by_id = by_id(gold_items, "retrieval gold")
    actual_by_id = by_id(actual_items, "retrieval results")
    if set(gold_by_id) != set(actual_by_id):
        missing = sorted(set(gold_by_id) - set(actual_by_id))
        extra = sorted(set(actual_by_id) - set(gold_by_id))
        raise ValueError(
            f"retrieval result coverage mismatch: missing={missing}, extra={extra}"
        )
    paired: list[dict[str, Any]] = []
    required_groups_by_id: dict[str, tuple[frozenset[str], ...]] = {}
    forbidden_fields = {"ranking", "unauthorized_exposures", "visible_resources"}
    for item_id, gold in gold_by_id.items():
        leaked = forbidden_fields & set(gold)
        if leaked:
            raise ValueError(
                f"retrieval gold {item_id} contains actual-result fields: {sorted(leaked)}"
            )
        actual = actual_by_id[item_id]
        answerable = gold.get("answerable")
        relevance_value = gold.get("relevance")
        if not isinstance(answerable, bool) or not isinstance(
            relevance_value, Mapping
        ):
            raise ValueError(
                f"retrieval gold {item_id} requires boolean answerable and relevance"
            )
        relevance: dict[str, float] = {}
        for chunk_id, grade in relevance_value.items():
            if (
                not isinstance(chunk_id, str)
                or not chunk_id.strip()
                or isinstance(grade, bool)
                or not isinstance(grade, Real)
                or not math.isfinite(float(grade))
                or not 0.0 <= float(grade) <= 3.0
            ):
                raise ValueError(
                    f"retrieval gold {item_id} relevance grades are invalid"
                )
            relevance[chunk_id] = float(grade)
        if answerable != any(grade > 0.0 for grade in relevance.values()):
            raise ValueError(
                f"retrieval gold {item_id} relevance disagrees with answerable"
            )
        if set(actual) - {"id", "ranking", "visible_resources"}:
            raise ValueError(f"retrieval result {item_id} contains unknown fields")
        ranking = actual.get("ranking")
        visible = actual.get("visible_resources")
        if not isinstance(ranking, list) or not isinstance(visible, list):
            raise ValueError(
                f"retrieval result {item_id} requires ranking and visible_resources lists"
            )
        if any(not isinstance(value, str) for value in ranking):
            raise ValueError(
                f"retrieval result {item_id} ranking IDs must be text"
            )
        ranking_ids = [value.strip() for value in ranking]
        if any(not value for value in ranking_ids) or len(ranking_ids) != len(set(ranking_ids)):
            raise ValueError(
                f"retrieval result {item_id} ranking IDs must be unique and non-empty"
            )
        visible_ids: set[str] = set()
        for index, event in enumerate(visible):
            if not isinstance(event, dict) or set(event) != {"stage", "kind", "id"}:
                raise ValueError(
                    f"retrieval result {item_id} visible_resources[{index}] is invalid"
                )
            if any(
                not isinstance(event[field], str)
                for field in ("stage", "kind", "id")
            ):
                raise ValueError(
                    f"retrieval result {item_id} visible resource fields must be text"
                )
            stage = event["stage"].strip()
            kind = event["kind"].strip()
            resource_id = event["id"].strip()
            if not stage or not kind or not resource_id:
                raise ValueError(
                    f"retrieval result {item_id} visible resource fields are required"
                )
            visible_ids.add(resource_id)
        forbidden = {
            str(value).strip() for value in gold.get("forbidden_chunk_ids", [])
        }
        positive = {
            chunk_id for chunk_id, grade in relevance.items() if grade > 0.0
        }
        groups_value = gold.get("required_evidence_groups")
        if groups_value is None:
            groups_value = [[chunk_id] for chunk_id in sorted(positive)]
        if not isinstance(groups_value, list):
            raise ValueError(
                f"retrieval gold {item_id} required_evidence_groups must be a list"
            )
        groups: list[frozenset[str]] = []
        for index, group in enumerate(groups_value):
            if not isinstance(group, list):
                raise ValueError(
                    f"retrieval gold {item_id} evidence group {index} must be a list"
                )
            if any(not isinstance(value, str) for value in group):
                raise ValueError(
                    f"retrieval gold {item_id} evidence group {index} must contain text IDs"
                )
            normalized = frozenset(value.strip() for value in group)
            if (
                not normalized
                or any(not value for value in normalized)
                or len(normalized) != len(group)
                or not normalized <= positive
            ):
                raise ValueError(
                    f"retrieval gold {item_id} evidence group {index} is invalid"
                )
            groups.append(normalized)
        if bool(gold.get("answerable")) != bool(groups):
            raise ValueError(
                f"retrieval gold {item_id} evidence groups disagree with answerable"
            )
        required_groups_by_id[item_id] = tuple(groups)
        paired.append(
            {
                "id": item_id,
                "answerable": answerable,
                "relevance": relevance,
                "ranking": ranking_ids,
                "unauthorized_exposures": sorted(visible_ids & forbidden),
            }
        )
    metrics = evaluate_retrieval_items(paired)
    complete_queries: list[float] = []
    for item in paired:
        if not item["answerable"]:
            continue
        retrieved = set(item["ranking"][:5])
        complete_queries.append(
            float(
                all(
                    bool(retrieved & group)
                    for group in required_groups_by_id[item["id"]]
                )
            )
        )
    return replace(
        metrics,
        recall_at_5=math.fsum(complete_queries) / len(complete_queries),
    )


def evaluate_retrieval_dataset(path: str | Path) -> RetrievalMetrics:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("dataset_id") != "gold-v1":
        raise ValueError("retrieval dataset_id must be gold-v1")
    if not str(payload.get("version", "")).strip():
        raise ValueError("retrieval dataset version is required")
    if not str(payload.get("owner", "")).strip():
        raise ValueError("retrieval dataset owner is required")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("retrieval dataset items must be a list")
    ids: set[str] = set()
    quotas = {
        question_class: {"success": 0, "boundary": 0}
        for question_class in QUESTION_CLASSES
    }
    for item in items:
        item_id = str(item.get("id", "")).strip()
        if not item_id or item_id in ids:
            raise ValueError("retrieval item IDs must be unique and non-empty")
        ids.add(item_id)
        question_class = item.get("question_class")
        case_type = item.get("case_type")
        if question_class not in QUESTION_CLASSES:
            raise ValueError(f"unknown question class: {question_class}")
        if case_type not in {"success", "boundary"}:
            raise ValueError(f"invalid case type: {case_type}")
        if not isinstance(item.get("answerable"), bool):
            raise ValueError(f"item {item_id} answerable must be boolean")
        if not str(item.get("query", "")).strip():
            raise ValueError(f"item {item_id} query must not be empty")
        if not isinstance(item.get("relevance"), dict):
            raise ValueError(f"item {item_id} relevance must be an object")
        if not isinstance(item.get("ranking"), list):
            raise ValueError(f"item {item_id} ranking must be a list")
        if not isinstance(item.get("unauthorized_exposures"), list):
            raise ValueError(f"item {item_id} exposures must be a list")
        relevance = item["relevance"]
        if any(
            not str(chunk_id).strip()
            or isinstance(grade, bool)
            or not isinstance(grade, Real)
            or not math.isfinite(float(grade))
            or not 0.0 <= float(grade) <= 3.0
            for chunk_id, grade in relevance.items()
        ):
            raise ValueError(f"item {item_id} relevance grades must be in [0, 3]")
        ranking = [str(value).strip() for value in item["ranking"]]
        if any(not value for value in ranking) or len(ranking) != len(set(ranking)):
            raise ValueError(f"item {item_id} ranking IDs must be unique and non-empty")
        exposures = [str(value).strip() for value in item["unauthorized_exposures"]]
        if any(not value for value in exposures) or len(exposures) != len(set(exposures)):
            raise ValueError(f"item {item_id} exposure IDs must be unique and non-empty")
        if not item["answerable"] and _positive_relevant(item):
            raise ValueError(f"unanswerable item {item_id} has positive relevance")
        quotas[str(question_class)][str(case_type)] += 1
    for question_class, counts in quotas.items():
        if counts["success"] < 5 or counts["boundary"] < 2:
            raise ValueError(
                f"{question_class} requires five success and two boundary cases"
            )
    return evaluate_retrieval_items(items)
