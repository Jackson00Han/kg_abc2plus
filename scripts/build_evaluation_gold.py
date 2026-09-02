#!/usr/bin/env python3
"""Build/check the split, versioned Stage 8 evaluation-gold manifest."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "evaluation" / "gold-v1"
GRAPH_REVIEW = ROOT / "evaluation" / "graph-review-v1.json"
GRAPH_RESULTS = ROOT / "evaluation" / "observations" / "graph-system-v1.json"
QUESTIONS = ROOT / "datasets" / "dev-corpus-v1" / "questions.jsonl"
ANSWERS = ROOT / "datasets" / "dev-corpus-v1" / "answers.jsonl"
CHUNKS = ROOT / "datasets" / "dev-corpus-v1" / "chunks.jsonl"
CORPUS_MANIFEST = ROOT / "datasets" / "dev-corpus-v1" / "manifest.json"
CONTRACT = ROOT / "contracts" / "acceptance.v1.json"


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _jsonl(items: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical(item) for item in items)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_id(kind: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sample-graphrag:{kind}:{value}"))


def _conflict_source(
    source_key: str,
    text: str,
    *,
    title: str,
    published_at: str,
) -> dict[str, Any]:
    checksum = _sha256_bytes(text.encode("utf-8"))
    canonical_uri = f"urn:sample-graphrag:evaluation-conflict:{source_key}"
    document_id = _stable_id("document", canonical_uri)
    version_id = _stable_id("version", f"{document_id}:{checksum}")
    chunk_id = _stable_id("chunk", f"{version_id}:0:{checksum}")
    return {
        "canonical_uri": canonical_uri,
        "char_end": len(text),
        "char_start": 0,
        "chunk_checksum": checksum,
        "chunk_id": chunk_id,
        "document_id": document_id,
        "document_title": title,
        "ordinal": 0,
        "page_number": 1,
        "published_at": published_at,
        "section": "Revenue",
        "source_name": "Stage 8 deterministic conflict fixture",
        "text": text,
        "version_checksum": checksum,
        "version_id": version_id,
        "version_number": 1,
    }


def _evidence(source: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key != "text"}


def _claim(
    case_id: str,
    position: int,
    text: str,
    sources: list[dict[str, Any]],
    *,
    exact_tokens: list[str],
    inference: bool = False,
    comparison: dict[str, str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "claim_id": f"{case_id}:claim-{position:02d}",
        "evidence_chunk_ids": [source["chunk_id"] for source in sources],
        "exact_tokens": exact_tokens,
        "inference": inference,
        "material": True,
        "reference_text": text,
        "required_terms": [
            token
            for token in ("Apple Inc.", *exact_tokens)
            if token in text
        ],
    }
    if comparison is not None:
        item["comparison"] = comparison
    return item


def _conflict_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    same_a = _conflict_source(
        "same-scope-source-a",
        "Apple Inc. reported revenue of $10 million for fiscal year 2024.",
        title="Apple FY2024 revenue statement A",
        published_at="2024-11-01T00:00:00+00:00",
    )
    same_b = _conflict_source(
        "same-scope-source-b",
        "Apple Inc. reported revenue of $12 million for fiscal year 2024.",
        title="Apple FY2024 revenue statement B",
        published_at="2024-11-02T00:00:00+00:00",
    )
    period_a = _conflict_source(
        "different-period-source-a",
        "Apple Inc. reported revenue of $10 million for fiscal year 2023.",
        title="Apple FY2023 revenue statement",
        published_at="2023-11-01T00:00:00+00:00",
    )
    period_b = _conflict_source(
        "different-period-source-b",
        "Apple Inc. reported revenue of $12 million for fiscal year 2024.",
        title="Apple FY2024 revenue statement",
        published_at="2024-11-01T00:00:00+00:00",
    )
    conflict_id = "same_scope_conflict-success-01"
    conflict_claims = [
        _claim(
            conflict_id,
            1,
            same_a["text"],
            [same_a],
            exact_tokens=["$10 million", "fiscal year 2024"],
        ),
        _claim(
            conflict_id,
            2,
            same_b["text"],
            [same_b],
            exact_tokens=["$12 million", "fiscal year 2024"],
        ),
    ]
    contrast_id = "different_periods_not_conflict-boundary-01"
    contrast_inference = (
        "Revenue for Apple Inc. increased from fiscal year 2023 to fiscal year 2024."
    )
    contrast_claims = [
        _claim(
            contrast_id,
            1,
            period_a["text"],
            [period_a],
            exact_tokens=["$10 million", "fiscal year 2023"],
        ),
        _claim(
            contrast_id,
            2,
            period_b["text"],
            [period_b],
            exact_tokens=["$12 million", "fiscal year 2024"],
        ),
        _claim(
            contrast_id,
            3,
            contrast_inference,
            [period_a, period_b],
            exact_tokens=[],
            inference=True,
            comparison={
                "direction": "increased",
                "from_period": "fiscal year 2023",
                "from_value": "$10 million",
                "to_period": "fiscal year 2024",
                "to_value": "$12 million",
            },
        ),
    ]
    cases = [
        {
            "case_type": "success",
            "claims": conflict_claims,
            "conflict": {"required": True},
            "corpus_id": "evaluation-conflict-corpus-v1",
            "corpus_version": "1.0.0",
            "evidence": [_evidence(same_a), _evidence(same_b)],
            "expected_material_claim_count": 2,
            "expected_status": "conflict",
            "forbidden_answer_terms": [],
            "gold_version": "2.0.0",
            "id": conflict_id,
            "query": "What revenue did Apple report for fiscal year 2024?",
            "question_class": "temporal_conflict",
            "reference_answer": "Conflicting source statements: "
            + " ".join(claim["reference_text"] for claim in conflict_claims),
            "refusal_reason": None,
            "required_exact_tokens": [
                "$10 million",
                "fiscal year 2024",
                "$12 million",
            ],
            "temporal_comparison": None,
        },
        {
            "case_type": "boundary",
            "claims": contrast_claims,
            "conflict": None,
            "corpus_id": "evaluation-conflict-corpus-v1",
            "corpus_version": "1.0.0",
            "evidence": [_evidence(period_a), _evidence(period_b)],
            "expected_material_claim_count": 3,
            "expected_status": "answered",
            "forbidden_answer_terms": [],
            "gold_version": "2.0.0",
            "id": contrast_id,
            "query": "Compare Apple's revenue in fiscal years 2023 and 2024.",
            "question_class": "temporal_conflict",
            "reference_answer": " ".join(
                (
                    period_a["text"],
                    period_b["text"],
                    f"Inference: {contrast_inference}",
                )
            ),
            "refusal_reason": None,
            "required_exact_tokens": [
                "$10 million",
                "fiscal year 2023",
                "$12 million",
                "fiscal year 2024",
            ],
            "temporal_comparison": {
                "inference_direction": "increased",
                "must_label_inference": True,
                "required": True,
                "required_periods": ["fiscal year 2023", "fiscal year 2024"],
            },
        },
    ]
    return cases, [same_a, same_b, period_a, period_b]


def _evaluation_questions(
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add prediction-free, claim-derived evidence requirements to questions."""
    answers_by_id = {item["id"]: item for item in answers}
    if len(answers_by_id) != len(answers) or set(answers_by_id) != {
        item["id"] for item in questions
    }:
        raise ValueError("question and answer IDs must match exactly")
    result: list[dict[str, Any]] = []
    for question in questions:
        answer = answers_by_id[question["id"]]
        groups: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for claim in answer["claims"]:
            if claim.get("material") is not True:
                continue
            evidence_ids = tuple(
                dict.fromkeys(str(value) for value in claim["evidence_chunk_ids"])
            )
            candidates = (
                [(chunk_id,) for chunk_id in evidence_ids]
                if claim.get("inference") is True
                else [evidence_ids]
            )
            for group in candidates:
                if not group or group in seen:
                    continue
                seen.add(group)
                groups.append(list(group))
        if bool(question["answerable"]) != bool(groups):
            raise ValueError(
                f"question {question['id']} answerability disagrees with claim evidence"
            )
        result.append({**question, "required_evidence_groups": groups})
    return result


def build_files() -> dict[Path, bytes]:
    review = _load_json(GRAPH_REVIEW)
    graph_gold_items: list[dict[str, Any]] = []
    graph_results: list[dict[str, Any]] = []
    for item in review["items"]:
        kind = item["kind"]
        expected = (
            {"adjudicated_correct": item["adjudicated_correct"]}
            if kind == "entity"
            else {"adjudicated_supported": item["adjudicated_supported"]}
            if kind == "relationship"
            else {"outcome": item["expected_outcome"]}
        )
        graph_gold_items.append(
            {
                "evidence_ids": item["evidence_ids"],
                "expected": expected,
                "id": item["id"],
                "kind": kind,
                "negative_case": item["negative_case"],
            }
        )
        graph_results.append(
            {
                "accepted": item["system_accepted"],
                "id": item["id"],
                "kind": kind,
            }
            if kind in {"entity", "relationship"}
            else {
                "id": item["id"],
                "kind": kind,
                "predicted_outcome": item["predicted_outcome"],
            }
        )
    graph_gold = {
        "adjudication_policy": review["adjudication_policy"],
        "contains_predictions": False,
        "dataset_id": "graph-review-v1",
        "items": graph_gold_items,
        "owner": review["owner"],
        "version": "2.0.0",
    }
    graph_actual = {
        "contains_adjudication": False,
        "dataset_id": "graph-review-v1-observations",
        "items": graph_results,
        "producer": "stage4-governance-review-v1",
        "version": "1.0.0",
    }
    conflict_cases, conflict_sources = _conflict_fixture()
    files = {
        OUTPUT_DIR / "graph.json": _canonical(graph_gold),
        OUTPUT_DIR / "conflict-answers.jsonl": _jsonl(conflict_cases),
        OUTPUT_DIR / "conflict-sources.jsonl": _jsonl(conflict_sources),
        GRAPH_RESULTS: _canonical(graph_actual),
    }

    source_questions = _load_jsonl(QUESTIONS)
    answers = _load_jsonl(ANSWERS)
    questions = _evaluation_questions(source_questions, answers)
    files[OUTPUT_DIR / "questions.jsonl"] = _jsonl(questions)
    quotas: Counter[tuple[str, str]] = Counter(
        (item["question_class"], item["case_type"]) for item in questions
    )
    question_classes = {
        question_class: {
            "boundary": quotas[(question_class, "boundary")],
            "success": quotas[(question_class, "success")],
        }
        for question_class in sorted({item["question_class"] for item in questions})
    }
    role_paths = {
        "acceptance_contract": CONTRACT,
        "corpus_manifest": CORPUS_MANIFEST,
        "questions": OUTPUT_DIR / "questions.jsonl",
        "answers": ANSWERS,
        "chunks": CHUNKS,
        "graph_gold": OUTPUT_DIR / "graph.json",
        "conflict_answers": OUTPUT_DIR / "conflict-answers.jsonl",
        "conflict_sources": OUTPUT_DIR / "conflict-sources.jsonl",
    }
    counts = {
        "acceptance_contract": 1,
        "corpus_manifest": 1,
        "questions": len(questions),
        "answers": len(answers),
        "chunks": len(_load_jsonl(CHUNKS)),
        "graph_gold": len(graph_gold_items),
        "conflict_answers": len(conflict_cases),
        "conflict_sources": len(conflict_sources),
    }

    def bytes_for(path: Path) -> bytes:
        return files[path] if path in files else path.read_bytes()

    artifacts = [
        {
            "item_count": counts[role],
            "path": str(path.relative_to(ROOT)),
            "role": role,
            "sha256": _sha256_bytes(bytes_for(path)),
        }
        for role, path in role_paths.items()
    ]
    required_ids = sorted(item["id"] for item in questions)
    coverage = {
        "case_set_sha256": _sha256_bytes(_canonical(required_ids)),
        "conflict_case_ids": sorted(item["id"] for item in conflict_cases),
        "question_classes": question_classes,
        "required_case_ids": required_ids,
        "unauthorized_case_ids": sorted(
            item["id"]
            for item in questions
            if item["question_class"] == "unauthorized"
        ),
        "unanswerable_case_ids": sorted(
            item["id"]
            for item in questions
            if item["question_class"] == "unanswerable"
        ),
    }
    manifest = {
        "adjudication": {
            "contains_predictions": False,
            "evidence_unit": "chunk",
            "judgment_policy": "exhaustive_within_bounded_corpus",
        },
        "artifacts": artifacts,
        "coverage": coverage,
        "dataset_id": "gold-v1",
        "owner": "repository-maintainers",
        "repository_root": "../..",
        "schema_version": "evaluation-gold-manifest-v1",
        "version": "2.0.0",
    }
    files[OUTPUT_DIR / "manifest.json"] = _canonical(manifest)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = build_files()
    drift: list[str] = []
    for path, expected in files.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != expected:
                drift.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
    if drift:
        raise SystemExit("evaluation gold drift: " + ", ".join(drift))
    print(
        "evaluation gold is reproducible"
        if args.check
        else "wrote evaluation/gold-v1 and split graph observations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
