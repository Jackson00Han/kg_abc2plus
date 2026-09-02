"""Fail-closed loaders for the versioned, prediction-free evaluation gold."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .answers import CITATION_LOCATION_FIELDS, validate_gold_dataset
from .models import GoldDataset


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
PREDICTION_FIELDS = frozenset(
    {
        "actual_result",
        "accepted",
        "predicted_outcome",
        "prediction",
        "ranking",
        "result",
        "system_accepted",
        "unauthorized_exposures",
        "visible_resources",
    }
)


def _has_hitting_set(
    groups: tuple[frozenset[str], ...], *, maximum_size: int
) -> bool:
    """Return whether all AND-of-OR evidence groups fit inside the top-k bound."""

    def search(
        remaining: tuple[frozenset[str], ...], budget: int
    ) -> bool:
        if not remaining:
            return True
        if budget == 0:
            return False
        branch = min(remaining, key=len)
        return any(
            search(
                tuple(group for group in remaining if candidate not in group),
                budget - 1,
            )
            for candidate in branch
        )

    return search(groups, maximum_size)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_jsonl(path: str | Path) -> tuple[dict[str, Any], ...]:
    path = Path(path)
    values: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain an object")
            values.append(value)
    return tuple(values)


def _safe_artifact_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("gold artifact path must be non-empty text")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("gold artifact paths must be repository-relative")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("gold artifact path escapes the repository") from exc
    if not resolved.is_file():
        raise ValueError(f"gold artifact does not exist: {value}")
    return resolved


def _artifact_paths(
    manifest: Mapping[str, Any], root: Path
) -> dict[str, Path]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("gold manifest requires artifacts")
    result: dict[str, Path] = {}
    for index, item in enumerate(artifacts):
        if not isinstance(item, Mapping):
            raise ValueError(f"gold artifact {index} must be an object")
        if set(item) != {"role", "path", "sha256", "item_count"}:
            raise ValueError(f"gold artifact {index} fields are invalid")
        role = item.get("role")
        checksum = item.get("sha256")
        count = item.get("item_count")
        if (
            not isinstance(role, str)
            or not role.strip()
            or role in result
            or not isinstance(checksum, str)
            or len(checksum) != 64
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
        ):
            raise ValueError(f"gold artifact {index} metadata is invalid")
        path = _safe_artifact_path(root, item.get("path"))
        actual_checksum = sha256_file(path)
        if actual_checksum != checksum:
            raise ValueError(
                f"gold artifact checksum mismatch for {item.get('path')}"
            )
        result[role] = path
    required = {
        "acceptance_contract",
        "corpus_manifest",
        "questions",
        "answers",
        "chunks",
        "graph_gold",
        "conflict_answers",
        "conflict_sources",
    }
    if set(result) != required:
        raise ValueError("gold manifest artifact roles are incomplete")
    return result


def _unique(items: tuple[dict[str, Any], ...], name: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip() or item_id in result:
            raise ValueError(f"{name} IDs must be unique and non-empty")
        result[item_id] = item
    return result


def _validate_questions_and_answers(
    questions: tuple[dict[str, Any], ...],
    answers: tuple[dict[str, Any], ...],
    chunks: tuple[dict[str, Any], ...],
    coverage: Mapping[str, Any],
) -> None:
    question_by_id = _unique(questions, "question gold")
    answer_by_id = _unique(answers, "answer gold")
    chunk_by_id = {
        item.get("chunk_id"): item
        for item in chunks
        if isinstance(item.get("chunk_id"), str)
    }
    if len(chunk_by_id) != len(chunks):
        raise ValueError("Chunk gold IDs must be unique and non-empty")
    if set(question_by_id) != set(answer_by_id):
        raise ValueError("question and answer gold IDs must match exactly")
    required_ids = coverage.get("required_case_ids")
    if not isinstance(required_ids, list) or required_ids != sorted(question_by_id):
        raise ValueError("gold manifest required_case_ids do not match the corpus")

    quotas: Counter[tuple[str, str]] = Counter()
    for item_id, question in question_by_id.items():
        forbidden_prediction_fields = PREDICTION_FIELDS & set(question)
        if forbidden_prediction_fields:
            raise ValueError(
                f"question gold {item_id} contains prediction fields: "
                f"{sorted(forbidden_prediction_fields)}"
            )
        question_class = question.get("question_class")
        case_type = question.get("case_type")
        if not isinstance(question.get("answerable"), bool):
            raise ValueError(f"question gold {item_id} answerable must be boolean")
        if question_class not in QUESTION_CLASSES or case_type not in {
            "success",
            "boundary",
        }:
            raise ValueError(f"question gold {item_id} has invalid classification")
        quotas[(str(question_class), str(case_type))] += 1
        if question.get("query") != answer_by_id[item_id].get("query"):
            raise ValueError(f"question and answer text differ for {item_id}")
        if question_class != answer_by_id[item_id].get("question_class"):
            raise ValueError(f"question and answer class differ for {item_id}")
        relevance = question.get("relevance")
        if not isinstance(relevance, dict):
            raise ValueError(f"question gold {item_id} relevance is invalid")
        positive = {
            chunk_id for chunk_id, grade in relevance.items() if float(grade) > 0
        }
        answer_evidence = {
            item["chunk_id"] for item in answer_by_id[item_id].get("evidence", [])
        }
        if not answer_evidence <= positive:
            raise ValueError(f"answer gold {item_id} uses non-relevant evidence")
        if any(chunk_id not in chunk_by_id for chunk_id in relevance):
            raise ValueError(f"question gold {item_id} references an unknown Chunk")
        expected_groups: list[list[str]] = []
        seen_groups: set[tuple[str, ...]] = set()
        for claim in answer_by_id[item_id].get("claims", []):
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
                if group and group not in seen_groups:
                    seen_groups.add(group)
                    expected_groups.append(list(group))
        required_groups = question.get("required_evidence_groups")
        if required_groups != expected_groups:
            raise ValueError(
                f"question gold {item_id} required evidence groups are stale"
            )
        if bool(question.get("answerable")) != bool(required_groups):
            raise ValueError(
                f"question gold {item_id} evidence groups disagree with answerable"
            )
        for group in required_groups:
            if (
                not isinstance(group, list)
                or not group
                or len(group) != len(set(group))
                or not set(group) <= positive
            ):
                raise ValueError(
                    f"question gold {item_id} has an invalid evidence group"
                )
        frozen_groups = tuple(frozenset(group) for group in required_groups)
        if frozen_groups and not _has_hitting_set(frozen_groups, maximum_size=5):
            raise ValueError(
                f"question gold {item_id} cannot fit required evidence in top five"
            )

    declared_quotas = coverage.get("question_classes")
    actual_quotas = {
        question_class: {
            "success": quotas[(question_class, "success")],
            "boundary": quotas[(question_class, "boundary")],
        }
        for question_class in sorted(QUESTION_CLASSES)
    }
    if declared_quotas != actual_quotas:
        raise ValueError("gold manifest question-class quotas are stale")
    unauthorized_ids = sorted(
        item_id
        for item_id, question in question_by_id.items()
        if question["question_class"] == "unauthorized"
    )
    unanswerable_ids = sorted(
        item_id
        for item_id, question in question_by_id.items()
        if question["question_class"] == "unanswerable"
    )
    if coverage.get("unauthorized_case_ids") != unauthorized_ids:
        raise ValueError("unauthorized cases cannot be omitted from the manifest")
    if coverage.get("unanswerable_case_ids") != unanswerable_ids:
        raise ValueError("unanswerable cases cannot be omitted from the manifest")
    for item_id in unauthorized_ids:
        question = question_by_id[item_id]
        answer = answer_by_id[item_id]
        if not question.get("forbidden_chunk_ids"):
            raise ValueError(f"unauthorized gold {item_id} requires forbidden Chunks")
        if not answer.get("forbidden_answer_terms"):
            raise ValueError(f"unauthorized gold {item_id} requires forbidden answer terms")

    validate_gold_dataset(answers)
    for item_id, answer in answer_by_id.items():
        for evidence in answer["evidence"]:
            chunk = chunk_by_id[evidence["chunk_id"]]
            comparisons = {
                "chunk_checksum": chunk["checksum"],
                "document_id": chunk["document_id"],
                "version_id": chunk["version_id"],
                "ordinal": chunk["ordinal"],
                "char_start": chunk["char_start"],
                "char_end": chunk["char_end"],
                "page_number": chunk["page_number"],
                "section": chunk["section"],
            }
            if any(evidence[field] != value for field, value in comparisons.items()):
                raise ValueError(f"answer evidence range is stale for {item_id}")


def _validate_graph_gold(items: tuple[dict[str, Any], ...]) -> None:
    by_id = _unique(items, "graph gold")
    if len(by_id) < 50:
        raise ValueError("graph gold requires at least 50 items")
    kinds = {item.get("kind") for item in items}
    if kinds != {"entity", "relationship", "resolution"}:
        raise ValueError("graph gold must cover all three graph decision kinds")
    for item_id, item in by_id.items():
        if PREDICTION_FIELDS & set(item):
            raise ValueError(f"graph gold {item_id} contains a prediction")
        if not isinstance(item.get("negative_case"), bool) or not item.get(
            "evidence_ids"
        ):
            raise ValueError(f"graph gold {item_id} lacks adjudication evidence")
        expected = item.get("expected")
        if not isinstance(expected, dict):
            raise ValueError(f"graph gold {item_id} lacks an expected label")
    for kind in kinds:
        selected = [item for item in items if item["kind"] == kind]
        if not any(item["negative_case"] for item in selected) or not any(
            not item["negative_case"] for item in selected
        ):
            raise ValueError(f"graph gold {kind} needs positive and negative cases")


def _validate_conflict_gold(
    items: tuple[dict[str, Any], ...],
    sources: tuple[dict[str, Any], ...],
    coverage: Mapping[str, Any],
) -> None:
    by_id = _unique(items, "conflict answer gold")
    source_by_id = {
        item.get("chunk_id"): item
        for item in sources
        if isinstance(item.get("chunk_id"), str)
    }
    if len(source_by_id) != len(sources) or len(sources) < 4:
        raise ValueError("conflict sources require unique, non-empty Chunk IDs")
    if len(by_id) < 2 or coverage.get("conflict_case_ids") != sorted(by_id):
        raise ValueError("gold manifest must bind at least two conflict cases")
    if not any(item.get("expected_status") == "conflict" for item in items):
        raise ValueError("conflict gold requires a positive same-scope conflict")
    if not any(item.get("expected_status") == "answered" for item in items):
        raise ValueError("conflict gold requires a non-conflict contrast")
    for item_id, item in by_id.items():
        evidence = item.get("evidence")
        claims = item.get("claims")
        if not isinstance(evidence, list) or not isinstance(claims, list):
            raise ValueError(f"conflict gold {item_id} requires claims and evidence")
        for source in evidence:
            if any(field not in source for field in CITATION_LOCATION_FIELDS):
                raise ValueError(f"conflict gold {item_id} has incomplete provenance")
            source_record = source_by_id.get(source["chunk_id"])
            if source_record is None:
                raise ValueError(f"conflict gold {item_id} references an unknown source")
            text = source_record.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("conflict source text must be non-empty")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != source[
                "chunk_checksum"
            ]:
                raise ValueError(f"conflict source checksum is stale for {item_id}")
            if text[source["char_start"] : source["char_end"]] != text:
                raise ValueError(f"conflict source range is stale for {item_id}")
            if any(source.get(field) != source_record.get(field) for field in CITATION_LOCATION_FIELDS):
                raise ValueError(f"conflict source provenance is stale for {item_id}")
        if item.get("expected_status") == "conflict":
            provenance = {
                (source["document_id"], source["version_id"]) for source in evidence
            }
            if len(provenance) < 2 or len(claims) < 2:
                raise ValueError(
                    f"conflict gold {item_id} needs two distinct source versions"
                )


def load_gold_dataset(manifest_path: str | Path) -> GoldDataset:
    path = Path(manifest_path).resolve()
    manifest = load_json(path)
    if manifest.get("schema_version") != "evaluation-gold-manifest-v1":
        raise ValueError("gold manifest schema is invalid")
    if (
        manifest.get("dataset_id") != "gold-v1"
        or not isinstance(manifest.get("version"), str)
        or not str(manifest.get("version")).strip()
        or not isinstance(manifest.get("owner"), str)
        or not str(manifest.get("owner")).strip()
    ):
        raise ValueError("gold manifest identity is invalid")
    adjudication = manifest.get("adjudication")
    if adjudication != {
        "contains_predictions": False,
        "evidence_unit": "chunk",
        "judgment_policy": "exhaustive_within_bounded_corpus",
    }:
        raise ValueError("gold adjudication policy is invalid")
    root_value = manifest.get("repository_root")
    if root_value != "../..":
        raise ValueError("gold manifest repository_root is invalid")
    repository_root = (path.parent / root_value).resolve()
    artifacts = _artifact_paths(manifest, repository_root)
    artifact_metadata = {item["role"]: item for item in manifest["artifacts"]}

    questions = load_jsonl(artifacts["questions"])
    answers = load_jsonl(artifacts["answers"])
    chunks = load_jsonl(artifacts["chunks"])
    graph_payload = load_json(artifacts["graph_gold"])
    graph_items_value = graph_payload.get("items")
    if not isinstance(graph_items_value, list):
        raise ValueError("graph gold items must be a list")
    graph_items = tuple(graph_items_value)
    conflicts = load_jsonl(artifacts["conflict_answers"])
    conflict_sources = load_jsonl(artifacts["conflict_sources"])
    loaded_counts = {
        "questions": len(questions),
        "answers": len(answers),
        "chunks": len(chunks),
        "graph_gold": len(graph_items),
        "conflict_answers": len(conflicts),
        "conflict_sources": len(conflict_sources),
    }
    for role, count in loaded_counts.items():
        if artifact_metadata[role]["item_count"] != count:
            raise ValueError(f"gold artifact item_count is stale for {role}")

    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("gold manifest coverage is invalid")
    _validate_questions_and_answers(questions, answers, chunks, coverage)
    _validate_graph_gold(graph_items)
    _validate_conflict_gold(conflicts, conflict_sources, coverage)
    return GoldDataset(
        manifest=manifest,
        questions=questions,
        answers=answers,
        chunks=chunks,
        graph_items=graph_items,
        conflict_answers=conflicts,
        conflict_sources=conflict_sources,
        repository_root=str(repository_root),
    )
