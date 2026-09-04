"""Independent, answer-free semantic retrieval holdout validation.

The checked-in holdout was authored independently of the corpus builder and
contains reviewed query text and Chunk IDs only.
Live execution deliberately crosses the local authenticated HTTP boundary so
the deployment, rather than the gold asset, creates every query embedding.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import ipaddress
import json
import math
from numbers import Real
from pathlib import Path
import re
from typing import Any
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


HOLDOUT_MANIFEST_SCHEMA = "semantic-retrieval-holdout-manifest-v1"
HOLDOUT_RUN_SCHEMA = "semantic-retrieval-holdout-run-v1"
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
TRACE_STAGES = (
    "vector_recall",
    "bm25_recall",
    "seed_ranking",
    "graph_expansion",
    "candidate_vector_ranking",
    "final_ranking",
)
_QUESTION_FIELDS = frozenset(
    {
        "id",
        "question_class",
        "query",
        "answerable",
        "principal",
        "required_evidence_chunk_ids",
        "forbidden_chunk_ids",
    }
)
_PREDICTION_OR_ANSWER_FIELDS = frozenset(
    {
        "answer",
        "answers",
        "claims",
        "expected_answer",
        "forbidden_answer_terms",
        "query_embedding",
        "query_vector",
        "ranking",
        "result",
        "vector",
        "vector_id",
    }
)
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ITEM_ID = re.compile(
    r"^semantic-holdout-(?:single|cross|relationship|exact|temporal|"
    r"unanswerable|unauthorized)-[0-9]{2}$"
)
_TOKEN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_UNSAFE_NUMBER = re.compile(r"[$€£¥]|\d+(?:\.\d+)?\s*%")
_ANY_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")
_MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HoldoutPrincipal:
    principal_id: str
    tenant_id: str
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticHoldoutQuestion:
    item_id: str
    question_class: str
    query: str
    answerable: bool
    principal: HoldoutPrincipal
    required_evidence_chunk_ids: tuple[str, ...]
    forbidden_chunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticHoldoutDataset:
    manifest: Mapping[str, Any]
    questions: tuple[SemanticHoldoutQuestion, ...]
    manifest_path: Path
    repository_root: Path


@dataclass(frozen=True, slots=True)
class SemanticHoldoutMetrics:
    item_count: int
    answerable_count: int
    unanswerable_count: int
    unauthorized_count: int
    complete_evidence_recall_at_5: float
    evidence_id_recall_at_5: float
    mrr: float
    forbidden_exposure_count: int

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


JsonRequester = Callable[..., Mapping[str, Any]]


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_file(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty repository-relative text")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{field} must be repository-relative")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{field} escapes the repository") from error
    if not candidate.is_file():
        raise ValueError(f"{field} does not exist")
    return candidate


def _json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{field} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{field} must contain a JSON object")
    return value


def _jsonl_objects(path: Path, field: str) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"{field} is not valid UTF-8 text") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"{field}:{line_number} must not be blank")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{field}:{line_number} is invalid JSON") from error
        if not isinstance(value, dict):
            raise ValueError(f"{field}:{line_number} must contain an object")
        values.append(value)
    if not values:
        raise ValueError(f"{field} must not be empty")
    return tuple(values)


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _HEX_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _positive_integer(value: object, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return value


def _probability(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite number in [0, 1]")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{field} must be a finite number in [0, 1]")
    return result


def _required_text(value: object, field: str, *, maximum: int = 400) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{field} exceeds its safe text boundary")
    return normalized


def _normalized_words(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_TOKEN.findall(normalized))


def _normalized_query(value: str) -> str:
    return " ".join(_normalized_words(value))


def _shingles(words: tuple[str, ...], size: int) -> frozenset[tuple[str, ...]]:
    if len(words) < size:
        return frozenset()
    return frozenset(
        tuple(words[index : index + size])
        for index in range(len(words) - size + 1)
    )


def _check_manifest_shape(manifest: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "dataset_id",
        "version",
        "owner",
        "description",
        "authorship",
        "source_corpus",
        "artifact",
        "coverage",
        "execution",
        "novelty",
        "thresholds",
    }
    if set(manifest) != expected:
        raise ValueError("semantic holdout manifest fields are invalid")
    if manifest.get("schema_version") != HOLDOUT_MANIFEST_SCHEMA:
        raise ValueError("semantic holdout manifest schema is invalid")
    if manifest.get("dataset_id") != "semantic-holdout-v1":
        raise ValueError("semantic holdout dataset identity is invalid")
    for field in ("version", "owner", "description"):
        _required_text(manifest.get(field), field, maximum=1_000)
    if manifest.get("authorship") != "independently-authored-reviewed-holdout":
        raise ValueError("semantic holdout must declare independent reviewed authorship")


def _check_execution(execution: object) -> None:
    if not isinstance(execution, Mapping) or set(execution) != {
        "transport",
        "endpoint",
        "query_embedding_source",
        "gold_query_vectors_present",
        "request_fields",
        "allowed_embedding_providers",
    }:
        raise ValueError("semantic holdout execution contract is invalid")
    if (
        execution.get("transport") != "local_authenticated_http"
        or execution.get("endpoint") != "/v1/retrieval"
        or execution.get("query_embedding_source") != "server_live_provider"
        or execution.get("gold_query_vectors_present") is not False
        or execution.get("request_fields") != ["limits", "query_text"]
    ):
        raise ValueError("semantic holdout must use server-side live query embedding")
    providers = execution.get("allowed_embedding_providers")
    if (
        not isinstance(providers, list)
        or not providers
        or providers != sorted(set(providers))
        or any(not isinstance(item, str) or not item.strip() for item in providers)
        or any(
            "fixture" in item.casefold() or "deterministic" in item.casefold()
            for item in providers
        )
    ):
        raise ValueError("semantic holdout embedding provider allowlist is invalid")


def _bound_source_artifacts(
    manifest: Mapping[str, Any], repository_root: Path
) -> tuple[Path, Path, Path, Path]:
    source = manifest.get("source_corpus")
    if not isinstance(source, Mapping) or set(source) != {
        "dataset_id",
        "version",
        "manifest_path",
        "manifest_sha256",
        "chunks_path",
        "chunks_sha256",
        "legacy_questions_path",
        "legacy_questions_sha256",
    }:
        raise ValueError("semantic holdout source corpus binding is invalid")
    manifest_path = _safe_file(
        repository_root, source.get("manifest_path"), "source manifest path"
    )
    chunks_path = _safe_file(
        repository_root, source.get("chunks_path"), "source Chunks path"
    )
    legacy_path = _safe_file(
        repository_root,
        source.get("legacy_questions_path"),
        "legacy questions path",
    )
    expected = (
        (manifest_path, source.get("manifest_sha256"), "source manifest"),
        (chunks_path, source.get("chunks_sha256"), "source Chunks"),
        (legacy_path, source.get("legacy_questions_sha256"), "legacy questions"),
    )
    for path, checksum, field in expected:
        if _sha256(path) != _digest(checksum, f"{field} checksum"):
            raise ValueError(f"{field} checksum mismatch")
    source_manifest = _json_object(manifest_path, "source manifest")
    if (
        source_manifest.get("dataset_id") != source.get("dataset_id")
        or source_manifest.get("version") != source.get("version")
        or source_manifest.get("synthetic") is not True
    ):
        raise ValueError("semantic holdout source corpus identity is stale")
    return manifest_path, chunks_path, legacy_path, manifest_path.parent


def _question_artifact(
    manifest: Mapping[str, Any], repository_root: Path
) -> tuple[Path, int]:
    artifact = manifest.get("artifact")
    if not isinstance(artifact, Mapping) or set(artifact) != {
        "path",
        "sha256",
        "item_count",
    }:
        raise ValueError("semantic holdout question artifact is invalid")
    path = _safe_file(repository_root, artifact.get("path"), "question artifact path")
    if _sha256(path) != _digest(artifact.get("sha256"), "question artifact checksum"):
        raise ValueError("semantic holdout question artifact checksum mismatch")
    count = _positive_integer(artifact.get("item_count"), "item_count", maximum=20)
    if count < 12:
        raise ValueError("semantic holdout requires at least 12 cases")
    return path, count


def _chunk_index(
    rows: Sequence[Mapping[str, Any]], corpus_root: Path
) -> tuple[dict[str, Mapping[str, Any]], tuple[str, ...]]:
    chunks: dict[str, Mapping[str, Any]] = {}
    source_texts: list[str] = []
    source_cache: dict[Path, str] = {}
    for index, row in enumerate(rows):
        chunk_id = row.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip() or chunk_id in chunks:
            raise ValueError("source Chunk IDs must be unique and non-empty")
        tenant_id = row.get("tenant_id")
        groups = row.get("access_groups")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or not isinstance(groups, list)
            or not groups
            or groups != sorted(set(groups))
            or any(not isinstance(group, str) or not group.strip() for group in groups)
        ):
            raise ValueError(f"source Chunk {index} ACL is invalid")
        source_path = _safe_file(
            corpus_root, row.get("source_path"), "Chunk source path"
        )
        text = source_cache.get(source_path)
        if text is None:
            text = source_path.read_text(encoding="utf-8")
            source_cache[source_path] = text
        start = row.get("char_start")
        end = row.get("char_end")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not 0 <= start < end <= len(text)
        ):
            raise ValueError(f"source Chunk {chunk_id} range is invalid")
        chunks[chunk_id] = row
        source_texts.append(text[start:end])
    return chunks, tuple(source_texts)


def _principal(value: object, item_id: str) -> HoldoutPrincipal:
    if not isinstance(value, Mapping) or set(value) != {
        "principal_id",
        "tenant_id",
        "groups",
    }:
        raise ValueError(f"holdout {item_id} principal is invalid")
    principal_id = _required_text(value.get("principal_id"), "principal_id")
    tenant_id = _required_text(value.get("tenant_id"), "tenant_id")
    groups = value.get("groups")
    if (
        not isinstance(groups, list)
        or not groups
        or groups != sorted(set(groups))
        or any(not isinstance(item, str) or not item.strip() for item in groups)
    ):
        raise ValueError(f"holdout {item_id} groups are invalid")
    return HoldoutPrincipal(principal_id, tenant_id, tuple(groups))


def _id_list(value: object, field: str, item_id: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or value != sorted(set(value))
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"holdout {item_id} {field} must be a sorted unique list")
    return tuple(value)


def _ordered_id_list(value: object, field: str, item_id: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"holdout {item_id} {field} must be a unique ID list")
    return tuple(value)


def _validate_novel_query(
    query: str,
    legacy_queries: Sequence[str],
    source_texts: Sequence[str],
    novelty: Mapping[str, Any],
    item_id: str,
) -> None:
    maximum_sequence = _probability(
        novelty.get("maximum_legacy_sequence_similarity"),
        "maximum legacy sequence similarity",
    )
    maximum_jaccard = _probability(
        novelty.get("maximum_legacy_token_jaccard"),
        "maximum legacy token Jaccard",
    )
    legacy_shingle_size = _positive_integer(
        novelty.get("legacy_copy_shingle_words"),
        "legacy copy shingle words",
        maximum=20,
    )
    source_shingle_size = _positive_integer(
        novelty.get("source_copy_shingle_words"),
        "source copy shingle words",
        maximum=30,
    )
    if (
        maximum_sequence > 0.65
        or maximum_jaccard > 0.40
        or legacy_shingle_size > 5
        or source_shingle_size > 6
    ):
        raise ValueError("semantic holdout novelty guard is too permissive")
    normalized = _normalized_query(query)
    words = _normalized_words(query)
    word_set = set(words)
    for legacy in legacy_queries:
        legacy_normalized = _normalized_query(legacy)
        legacy_words = _normalized_words(legacy)
        if normalized == legacy_normalized:
            raise ValueError(f"holdout {item_id} duplicates a legacy question")
        sequence = SequenceMatcher(None, normalized, legacy_normalized).ratio()
        union = word_set | set(legacy_words)
        jaccard = 0.0 if not union else len(word_set & set(legacy_words)) / len(union)
        if sequence > maximum_sequence or jaccard > maximum_jaccard:
            raise ValueError(f"holdout {item_id} is too similar to a legacy question")
        if _shingles(words, legacy_shingle_size) & _shingles(
            legacy_words, legacy_shingle_size
        ):
            raise ValueError(f"holdout {item_id} copies a legacy question phrase")
    query_source_shingles = _shingles(words, source_shingle_size)
    if query_source_shingles and any(
        query_source_shingles & _shingles(_normalized_words(text), source_shingle_size)
        for text in source_texts
    ):
        raise ValueError(f"holdout {item_id} copies source prose")


def _validate_questions(
    raw_questions: Sequence[Mapping[str, Any]],
    *,
    expected_count: int,
    chunks: Mapping[str, Mapping[str, Any]],
    source_texts: Sequence[str],
    legacy_questions: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> tuple[SemanticHoldoutQuestion, ...]:
    if len(raw_questions) != expected_count:
        raise ValueError("semantic holdout item count is stale")
    novelty = manifest.get("novelty")
    if not isinstance(novelty, Mapping) or set(novelty) != {
        "maximum_legacy_sequence_similarity",
        "maximum_legacy_token_jaccard",
        "legacy_copy_shingle_words",
        "source_copy_shingle_words",
    }:
        raise ValueError("semantic holdout novelty contract is invalid")
    legacy_query_texts = []
    legacy_scopes: set[tuple[str, tuple[str, ...]]] = set()
    for item in legacy_questions:
        query = item.get("query")
        principal = item.get("principal")
        if not isinstance(query, str) or not isinstance(principal, Mapping):
            raise ValueError("legacy question fixture is invalid")
        groups = principal.get("groups")
        tenant_id = principal.get("tenant_id")
        if not isinstance(groups, list) or not isinstance(tenant_id, str):
            raise ValueError("legacy question principal is invalid")
        legacy_query_texts.append(query)
        legacy_scopes.add((tenant_id, tuple(sorted(str(group) for group in groups))))

    result: list[SemanticHoldoutQuestion] = []
    identifiers: set[str] = set()
    normalized_queries: set[str] = set()
    quotas: Counter[str] = Counter()
    for raw in raw_questions:
        if set(raw) != _QUESTION_FIELDS:
            leaked = _PREDICTION_OR_ANSWER_FIELDS & set(raw)
            if leaked:
                raise ValueError(
                    "semantic holdout contains answer/prediction fields: "
                    f"{sorted(leaked)}"
                )
            raise ValueError("semantic holdout question fields are invalid")
        item_id = _required_text(raw.get("id"), "holdout ID")
        if _ITEM_ID.fullmatch(item_id) is None or item_id in identifiers:
            raise ValueError("semantic holdout IDs must be unique and versioned")
        identifiers.add(item_id)
        question_class = raw.get("question_class")
        if question_class not in QUESTION_CLASSES:
            raise ValueError(f"holdout {item_id} question class is invalid")
        answerable = raw.get("answerable")
        if not isinstance(answerable, bool):
            raise ValueError(f"holdout {item_id} answerable must be boolean")
        query = _required_text(raw.get("query"), "query", maximum=300)
        if "\n" in query or "\r" in query:
            raise ValueError(f"holdout {item_id} query must be one line")
        if _UNSAFE_NUMBER.search(query):
            raise ValueError(f"holdout {item_id} query leaks a formatted answer value")
        numbers = set(_ANY_NUMBER.findall(query))
        if not numbers <= {"23", "24", "2023", "2024"}:
            raise ValueError(f"holdout {item_id} query contains an answer-like number")
        normalized_query = _normalized_query(query)
        if normalized_query in normalized_queries:
            raise ValueError("semantic holdout query text must be unique")
        normalized_queries.add(normalized_query)
        principal = _principal(raw.get("principal"), item_id)
        scope = (principal.tenant_id, principal.groups)
        if scope not in legacy_scopes:
            raise ValueError(f"holdout {item_id} has no local test persona")
        required = _id_list(
            raw.get("required_evidence_chunk_ids"),
            "required evidence IDs",
            item_id,
        )
        forbidden = _id_list(
            raw.get("forbidden_chunk_ids"), "forbidden IDs", item_id
        )
        if set(required) & set(forbidden):
            raise ValueError(f"holdout {item_id} evidence sets overlap")
        if any(chunk_id not in chunks for chunk_id in (*required, *forbidden)):
            raise ValueError(f"holdout {item_id} references an unknown Chunk")
        if len(required) > 5:
            raise ValueError(f"holdout {item_id} cannot fit evidence into top five")
        if question_class in {"unanswerable", "unauthorized"}:
            if answerable or required:
                raise ValueError(f"holdout {item_id} negative shape is invalid")
        elif not answerable or not required:
            raise ValueError(f"holdout {item_id} positive shape is invalid")
        if question_class == "unauthorized":
            if not forbidden:
                raise ValueError(f"holdout {item_id} requires forbidden Chunks")
        elif forbidden:
            raise ValueError(f"holdout {item_id} unexpectedly forbids Chunks")
        for chunk_id in required:
            chunk = chunks[chunk_id]
            if (
                chunk["tenant_id"] != principal.tenant_id
                or not set(chunk["access_groups"]) & set(principal.groups)
            ):
                raise ValueError(f"holdout {item_id} required evidence is unauthorized")
        for chunk_id in forbidden:
            chunk = chunks[chunk_id]
            if (
                chunk["tenant_id"] == principal.tenant_id
                and set(chunk["access_groups"]) & set(principal.groups)
            ):
                raise ValueError(f"holdout {item_id} forbidden evidence is authorized")
        _validate_novel_query(
            query,
            legacy_query_texts,
            source_texts,
            novelty,
            item_id,
        )
        quotas[str(question_class)] += 1
        result.append(
            SemanticHoldoutQuestion(
                item_id,
                str(question_class),
                query,
                answerable,
                principal,
                required,
                forbidden,
            )
        )
    if [item.item_id for item in result] != sorted(identifiers):
        raise ValueError("semantic holdout questions must be ordered by ID")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping) or set(coverage) != {
        "question_classes",
        "required_case_ids",
        "tenant_ids",
        "access_groups",
        "unanswerable_case_ids",
        "unauthorized_case_ids",
    }:
        raise ValueError("semantic holdout coverage is invalid")
    declared_quotas = coverage.get("question_classes")
    actual_quotas = {name: quotas[name] for name in sorted(QUESTION_CLASSES)}
    if declared_quotas != actual_quotas or any(
        value < 2 for value in actual_quotas.values()
    ):
        raise ValueError("semantic holdout question-class coverage is stale")
    if coverage.get("required_case_ids") != sorted(identifiers):
        raise ValueError("semantic holdout required case inventory is stale")
    if coverage.get("tenant_ids") != sorted({item.principal.tenant_id for item in result}):
        raise ValueError("semantic holdout tenant coverage is stale")
    if coverage.get("access_groups") != sorted(
        {group for item in result for group in item.principal.groups}
    ):
        raise ValueError("semantic holdout access-group coverage is stale")
    for question_class, field in (
        ("unanswerable", "unanswerable_case_ids"),
        ("unauthorized", "unauthorized_case_ids"),
    ):
        expected = sorted(
            item.item_id for item in result if item.question_class == question_class
        )
        if coverage.get(field) != expected:
            raise ValueError(f"semantic holdout {question_class} coverage is stale")
    return tuple(result)


def load_semantic_holdout(
    manifest_path: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> SemanticHoldoutDataset:
    """Load and fully validate the versioned holdout and source bindings."""

    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise ValueError("semantic holdout manifest does not exist")
    root = (
        path.parents[2]
        if repository_root is None
        else Path(repository_root).resolve()
    )
    if not root.is_dir():
        raise ValueError("repository root does not exist")
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("semantic holdout manifest is outside the repository") from error
    manifest = _json_object(path, "semantic holdout manifest")
    _check_manifest_shape(manifest)
    _check_execution(manifest.get("execution"))
    _check_thresholds(manifest.get("thresholds"))
    _, chunks_path, legacy_path, corpus_root = _bound_source_artifacts(manifest, root)
    question_path, expected_count = _question_artifact(manifest, root)
    chunk_rows = _jsonl_objects(chunks_path, "source Chunks")
    legacy_rows = _jsonl_objects(legacy_path, "legacy questions")
    chunks, source_texts = _chunk_index(chunk_rows, corpus_root)
    questions = _validate_questions(
        _jsonl_objects(question_path, "semantic holdout questions"),
        expected_count=expected_count,
        chunks=chunks,
        source_texts=source_texts,
        legacy_questions=legacy_rows,
        manifest=manifest,
    )
    return SemanticHoldoutDataset(manifest, questions, path, root)


def _check_thresholds(value: object) -> dict[str, float | int]:
    if not isinstance(value, Mapping) or set(value) != {
        "minimum_complete_evidence_recall_at_5",
        "minimum_evidence_id_recall_at_5",
        "minimum_mrr",
        "maximum_forbidden_exposure_count",
    }:
        raise ValueError("semantic holdout thresholds are invalid")
    maximum = value.get("maximum_forbidden_exposure_count")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise ValueError("maximum forbidden exposure count must be non-negative")
    checked: dict[str, float | int] = {
        "minimum_complete_evidence_recall_at_5": _probability(
            value.get("minimum_complete_evidence_recall_at_5"),
            "minimum complete evidence recall",
        ),
        "minimum_evidence_id_recall_at_5": _probability(
            value.get("minimum_evidence_id_recall_at_5"),
            "minimum evidence ID recall",
        ),
        "minimum_mrr": _probability(value.get("minimum_mrr"), "minimum MRR"),
        "maximum_forbidden_exposure_count": maximum,
    }
    if (
        checked["minimum_complete_evidence_recall_at_5"] < 0.70
        or checked["minimum_evidence_id_recall_at_5"] < 0.70
        or checked["minimum_mrr"] < 0.70
        or maximum != 0
    ):
        raise ValueError("semantic holdout thresholds are too permissive")
    return checked


def _result_index(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[tuple[str, ...], frozenset[str]]]:
    indexed: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {}
    for index, result in enumerate(results):
        if not isinstance(result, Mapping) or set(result) != {
            "id",
            "selected_chunk_ids",
            "visible_chunk_ids",
        }:
            raise ValueError(f"semantic holdout result {index} fields are invalid")
        item_id = _required_text(result.get("id"), "result ID")
        if item_id in indexed:
            raise ValueError("semantic holdout result IDs must be unique")
        selected = _ordered_id_list(
            result.get("selected_chunk_ids"), "selected IDs", item_id
        )
        visible = _id_list(result.get("visible_chunk_ids"), "visible IDs", item_id)
        if len(selected) > 20 or not set(selected) <= set(visible):
            raise ValueError(f"semantic holdout result {item_id} trace is invalid")
        indexed[item_id] = (selected, frozenset(visible))
    return indexed


def evaluate_semantic_holdout(
    dataset: SemanticHoldoutDataset,
    results: Sequence[Mapping[str, Any]],
) -> SemanticHoldoutMetrics:
    """Measure evidence-ID recall and ACL exposure without evaluating answers."""

    indexed = _result_index(results)
    expected_ids = {item.item_id for item in dataset.questions}
    if set(indexed) != expected_ids:
        raise ValueError("semantic holdout result coverage is incomplete")
    complete: list[float] = []
    evidence_hits = 0
    evidence_total = 0
    reciprocal_ranks: list[float] = []
    forbidden_exposures = 0
    for item in dataset.questions:
        selected, visible = indexed[item.item_id]
        top_five = selected[:5]
        required = set(item.required_evidence_chunk_ids)
        forbidden_exposures += len(visible & set(item.forbidden_chunk_ids))
        if not item.answerable:
            continue
        evidence_total += len(required)
        evidence_hits += len(required & set(top_five))
        complete.append(float(required <= set(top_five)))
        ranks = [
            rank
            for rank, chunk_id in enumerate(top_five, start=1)
            if chunk_id in required
        ]
        reciprocal_ranks.append(0.0 if not ranks else 1.0 / min(ranks))
    if not complete or evidence_total == 0:
        raise ValueError("semantic holdout requires answerable evidence cases")
    return SemanticHoldoutMetrics(
        item_count=len(dataset.questions),
        answerable_count=len(complete),
        unanswerable_count=sum(
            item.question_class == "unanswerable" for item in dataset.questions
        ),
        unauthorized_count=sum(
            item.question_class == "unauthorized" for item in dataset.questions
        ),
        complete_evidence_recall_at_5=math.fsum(complete) / len(complete),
        evidence_id_recall_at_5=evidence_hits / evidence_total,
        mrr=math.fsum(reciprocal_ranks) / len(reciprocal_ranks),
        forbidden_exposure_count=forbidden_exposures,
    )


def _metric_failures(
    metrics: SemanticHoldoutMetrics, thresholds: Mapping[str, Any]
) -> tuple[str, ...]:
    checked = _check_thresholds(thresholds)
    failures: list[str] = []
    for metric, threshold in (
        (
            "complete_evidence_recall_at_5",
            checked["minimum_complete_evidence_recall_at_5"],
        ),
        (
            "evidence_id_recall_at_5",
            checked["minimum_evidence_id_recall_at_5"],
        ),
        ("mrr", checked["minimum_mrr"]),
    ):
        if getattr(metrics, metric) < threshold:
            failures.append(f"{metric} below threshold")
    if metrics.forbidden_exposure_count > checked["maximum_forbidden_exposure_count"]:
        failures.append("forbidden_exposure_count above threshold")
    return tuple(failures)


def _loopback_base_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise ValueError("semantic holdout requires an explicit loopback IP") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not address.is_loopback
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("semantic holdout base URL must be an explicit loopback origin")
    return value.rstrip("/") + "/"


def _default_json_request(
    method: str,
    url: str,
    *,
    payload: Mapping[str, Any] | None,
    headers: Mapping[str, str],
    timeout: float,
) -> Mapping[str, Any]:
    body = None if payload is None else _canonical_json_bytes(dict(payload))
    request = Request(url, data=body, method=method, headers=dict(headers))
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise RuntimeError(f"local holdout request returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError("local holdout service is unavailable") from error
    if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
        raise RuntimeError("local holdout response exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("local holdout response is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise RuntimeError("local holdout response must be a JSON object")
    return value


def _trace_ids(
    trace: Mapping[str, Any], item_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected_value = trace.get("selected_chunk_ids")
    if not isinstance(selected_value, list):
        raise RuntimeError(f"holdout {item_id} retrieval trace has no selected IDs")
    selected = tuple(str(value).strip() for value in selected_value)
    if any(not value for value in selected) or len(selected) != len(set(selected)):
        raise RuntimeError(f"holdout {item_id} selected IDs are invalid")
    visible: set[str] = set(selected)
    for stage in TRACE_STAGES:
        hits = trace.get(stage)
        if not isinstance(hits, list):
            raise RuntimeError(f"holdout {item_id} retrieval trace is incomplete")
        for hit in hits:
            if not isinstance(hit, Mapping):
                raise RuntimeError(f"holdout {item_id} retrieval hit is invalid")
            chunk_id = hit.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                raise RuntimeError(f"holdout {item_id} retrieval hit has no Chunk ID")
            visible.add(chunk_id.strip())
    decisions = trace.get("decisions")
    if not isinstance(decisions, list):
        raise RuntimeError(f"holdout {item_id} retrieval decisions are incomplete")
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise RuntimeError(f"holdout {item_id} retrieval decision is invalid")
        chunk_id = decision.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            raise RuntimeError(f"holdout {item_id} decision has no Chunk ID")
        visible.add(chunk_id.strip())
    return selected, tuple(sorted(visible))


def run_live_semantic_holdout(
    dataset: SemanticHoldoutDataset,
    *,
    base_url: str = "http://127.0.0.1:8000",
    timeout_seconds: float = 30.0,
    requester: JsonRequester | None = None,
) -> dict[str, Any]:
    """Run holdout queries through the server-side live embedding boundary."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, Real)
        or not math.isfinite(float(timeout_seconds))
        or not 0.0 < float(timeout_seconds) <= 120.0
    ):
        raise ValueError("timeout_seconds must be finite and in (0, 120]")
    origin = _loopback_base_url(base_url)
    request_json = requester or _default_json_request
    bootstrap = request_json(
        "GET",
        urljoin(origin, "playground/bootstrap"),
        payload=None,
        headers={},
        timeout=float(timeout_seconds),
    )
    if bootstrap.get("schema_version") != "local-playground-bootstrap-v1":
        raise RuntimeError("local Playground bootstrap schema is incompatible")
    dataset_info = bootstrap.get("dataset")
    capabilities = bootstrap.get("capabilities")
    defaults = bootstrap.get("defaults")
    if not all(
        isinstance(value, Mapping)
        for value in (dataset_info, capabilities, defaults)
    ):
        raise RuntimeError("local Playground bootstrap is incomplete")
    source = dataset.manifest["source_corpus"]
    if (
        dataset_info.get("id") != source["dataset_id"]
        or dataset_info.get("version") != source["version"]
        or capabilities.get("custom_semantic_retrieval") is not True
    ):
        raise RuntimeError("local Playground does not serve the bound semantic corpus")
    embedding = dataset_info.get("embedding")
    if not isinstance(embedding, Mapping):
        raise RuntimeError("local Playground does not declare its embedding provider")
    provider = embedding.get("provider")
    allowed = dataset.manifest["execution"]["allowed_embedding_providers"]
    if not isinstance(provider, str) or provider not in allowed:
        raise RuntimeError("local Playground is not using an allowed live embedding provider")
    limits = defaults.get("retrieval_limits")
    personas = bootstrap.get("personas")
    if not isinstance(limits, Mapping) or not isinstance(personas, list):
        raise RuntimeError("local Playground retrieval configuration is unavailable")
    persona_by_scope: dict[tuple[str, tuple[str, ...]], str] = {}
    for persona in personas:
        if not isinstance(persona, Mapping):
            raise RuntimeError("local Playground persona is invalid")
        tenant_id = persona.get("tenant_id")
        groups = persona.get("groups")
        scopes = persona.get("scopes")
        persona_id = persona.get("id")
        if (
            not isinstance(tenant_id, str)
            or not isinstance(groups, list)
            or not isinstance(scopes, list)
            or "retrieval:read" not in scopes
            or not isinstance(persona_id, str)
        ):
            continue
        key = (tenant_id, tuple(sorted(str(group) for group in groups)))
        if key in persona_by_scope:
            raise RuntimeError("local Playground has duplicate persona scopes")
        persona_by_scope[key] = persona_id

    tokens: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    embedding_spaces: set[str] = set()
    for item in dataset.questions:
        key = (item.principal.tenant_id, item.principal.groups)
        persona_id = persona_by_scope.get(key)
        if persona_id is None:
            raise RuntimeError(
                f"holdout {item.item_id} has no matching local persona"
            )
        if persona_id not in tokens:
            session = request_json(
                "POST",
                urljoin(origin, "playground/session"),
                payload={"persona_id": persona_id},
                headers={},
                timeout=float(timeout_seconds),
            )
            token = session.get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("local Playground did not issue a session token")
            tokens[persona_id] = token
        response = request_json(
            "POST",
            urljoin(origin, dataset.manifest["execution"]["endpoint"].lstrip("/")),
            payload={"limits": dict(limits), "query_text": item.query},
            headers={"Authorization": f"Bearer {tokens[persona_id]}"},
            timeout=float(timeout_seconds),
        )
        trace = response.get("trace")
        if (
            not isinstance(trace, Mapping)
            or trace.get("tenant_id") != item.principal.tenant_id
        ):
            raise RuntimeError(f"holdout {item.item_id} returned an invalid tenant trace")
        embedding_space = trace.get("embedding_space_id")
        if not isinstance(embedding_space, str) or not embedding_space.strip():
            raise RuntimeError(f"holdout {item.item_id} has no embedding-space identity")
        embedding_spaces.add(embedding_space)
        selected, visible = _trace_ids(trace, item.item_id)
        results.append(
            {
                "id": item.item_id,
                "selected_chunk_ids": list(selected),
                "visible_chunk_ids": list(visible),
            }
        )
    metrics = evaluate_semantic_holdout(dataset, results)
    failures = _metric_failures(metrics, dataset.manifest["thresholds"])
    return {
        "schema_version": HOLDOUT_RUN_SCHEMA,
        "dataset_id": dataset.manifest["dataset_id"],
        "dataset_version": dataset.manifest["version"],
        "question_artifact_sha256": dataset.manifest["artifact"]["sha256"],
        "query_embedding_source": "server_live_provider",
        "embedding_provider": provider,
        "embedding_space_ids": sorted(embedding_spaces),
        "metrics": metrics.as_dict(),
        "thresholds": dict(dataset.manifest["thresholds"]),
        "passed": not failures,
        "failures": list(failures),
        "items": results,
    }


__all__ = [
    "HOLDOUT_MANIFEST_SCHEMA",
    "HOLDOUT_RUN_SCHEMA",
    "HoldoutPrincipal",
    "SemanticHoldoutDataset",
    "SemanticHoldoutMetrics",
    "SemanticHoldoutQuestion",
    "evaluate_semantic_holdout",
    "load_semantic_holdout",
    "run_live_semantic_holdout",
]
