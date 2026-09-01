"""Retrieval regression metrics with hand-computable definitions."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from numbers import Real
from pathlib import Path
from typing import Any, Sequence


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
    if not items:
        raise ValueError("retrieval dataset must not be empty")
    answerable = [item for item in items if item["answerable"]]
    if not answerable:
        raise ValueError("retrieval dataset must contain answerable items")
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for item in answerable:
        relevant = _positive_relevant(item)
        if not relevant:
            raise ValueError(f"answerable item {item['id']} has no relevant chunks")
        ranking = [str(value) for value in item["ranking"]]
        recalls.append(float(bool(set(ranking[:5]) & relevant)))
        reciprocal_ranks.append(_reciprocal_rank(ranking, relevant))
        ndcgs.append(_ndcg_at_k(item, 5))
    exposures = sum(len(item["unauthorized_exposures"]) for item in items)
    return RetrievalMetrics(
        item_count=len(items),
        answerable_count=len(answerable),
        recall_at_5=math.fsum(recalls) / len(recalls),
        mrr=math.fsum(reciprocal_ranks) / len(reciprocal_ranks),
        ndcg_at_5=math.fsum(ndcgs) / len(ndcgs),
        unauthorized_exposure_count=exposures,
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
