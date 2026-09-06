"""Executable, development-only acceptance contract for industrial knowledge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


QUESTION_CLASSES = frozenset({
    "identity", "exact_value", "structure", "diagnostic_evidence",
    "temporal_conflict", "product_applicability", "unanswerable", "unauthorized",
})
REQUIRED_METRICS = frozenset({
    "recall_at_5", "mrr", "ndcg_at_5", "citation_location_accuracy",
    "product_scope_violations", "authorization_violations", "graph_bound_violations",
})


def _integer(value: object, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
    return value


def validate_industrial_contract(value: Mapping[str, Any]) -> None:
    """Reject missing evidence/security classes and contradictory resource claims."""
    if value.get("schema_version") != "industrial-knowledge-contract-v1":
        raise ValueError("unsupported industrial contract schema")
    if value.get("profile") != "dev-mini" or value.get("production_candidate_eligible") is not False:
        raise ValueError("industrial development evidence cannot claim production qualification")
    corpus = value["corpus"]
    minimum = _integer(corpus["minimum_chunks"], "minimum_chunks", 300, 1000)
    _integer(corpus["maximum_chunks"], "maximum_chunks", minimum, 1000)
    _integer(corpus["minimum_authored_documents"], "minimum_authored_documents", 32, 1000)
    _integer(corpus["maximum_published_records"], "maximum_published_records", 1, 500)
    _integer(corpus["minimum_access_groups"], "minimum_access_groups", 3, 100)
    _integer(corpus["minimum_product_families"], "minimum_product_families", 2, 100)
    _integer(corpus["minimum_installed_assets"], "minimum_installed_assets", 8, 1000)
    if len(set(value["scope"]["core_families"])) < corpus["minimum_product_families"]:
        raise ValueError("product families do not satisfy corpus diversity")
    if not {"OFFICIAL_PUBLICATION", "CURATED_REFERENCE", "SYNTHETIC_FIELD_RECORD"} <= set(corpus["source_kinds"]):
        raise ValueError("source kinds must distinguish official, curated and synthetic material")
    for key in ("count_originals_separately_from_derivatives", "synthetic_is_not_real_field_evidence"):
        if corpus.get(key) is not True:
            raise ValueError(f"required source boundary: {key}")
    if corpus.get("routine_validation_requires_provider") is not False:
        raise ValueError("routine validation must remain offline")
    cases = value["question_classes"]
    if len(cases) != len(QUESTION_CLASSES) or {item["id"] for item in cases} != QUESTION_CLASSES:
        raise ValueError("all industrial question classes must be retained")
    if any(not str(item.get(key, "")).strip() for item in cases for key in ("positive", "negative")):
        raise ValueError("every question class needs positive and negative cases")
    evaluation = value["evaluation"]
    _integer(evaluation["minimum_cases"], "minimum_cases", 48, 10000)
    if not evaluation.get("dataset_owner"):
        raise ValueError("evaluation requires an accountable dataset owner")
    if evaluation.get("negative_cases_must_be_reported") is not True:
        raise ValueError("negative cases cannot be excluded")
    if evaluation.get("holdout_predictions_are_independent") is not True:
        raise ValueError("gold and predictions must be independent")
    metrics = evaluation["metrics"]
    if len(metrics) != len(REQUIRED_METRICS) or {item["id"] for item in metrics} != REQUIRED_METRICS:
        raise ValueError("all quality, evidence, scope and security metrics are required")
    for item in metrics:
        if not item.get("measurement") or item.get("operator") not in {"gte", "eq"}:
            raise ValueError("every metric needs an executable measurement and comparison")
        target = item["target"]
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not 0 <= target <= 1:
            raise ValueError("metric targets must be finite ratios or zero violations")
        if item["id"].endswith("_violations") and (target != 0 or item["operator"] != "eq"):
            raise ValueError("scope, authorization and bounds permit zero violations")
        if item["id"] == "citation_location_accuracy" and (target != 1 or item["operator"] != "eq"):
            raise ValueError("every citation must resolve exactly")
    resources = value["resources"]
    if resources["online_source_bytes"] != 5 * 1024 * 1024:
        raise ValueError("industrial work must preserve the existing online byte cap")
    _integer(resources["offline_original_bytes"], "offline_original_bytes", resources["online_source_bytes"], 96 * 1024 * 1024)
    _integer(resources["selected_pdf_pages_per_normalization"], "selected_pdf_pages_per_normalization", 1, 24)
    _integer(resources["maximum_graph_expansion_hops"], "maximum_graph_expansion_hops", 1, 2)
    _integer(resources["maximum_graph_nodes_per_response"], "maximum_graph_nodes_per_response", 1, 150)
    _integer(resources["maximum_graph_edges_per_response"], "maximum_graph_edges_per_response", 1, 200)
    if resources["maximum_concurrent_load_jobs"] != 1:
        raise ValueError("dev-mini industrial loading uses one bounded job")
    if any(flag is not True for flag in value["delivery"].values()):
        raise ValueError("industrial delivery requirements cannot be disabled")


def load_industrial_contract(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_industrial_contract(value)
    return value
