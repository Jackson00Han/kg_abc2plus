"""Pinned industrial retrieval captures and source-checked, answer-free metrics.

Gold annotations never enter retrieval requests. Complete alternative evidence
sets are measured separately from standard ranked relevance metrics.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from graphrag_prod.domain.ids import chunk_id, content_checksum, document_id, version_id
from graphrag_prod.industrial.provenance import canonical_json, digest
from graphrag_prod.retrieval.metrics import evaluate_retrieval_items
from graphrag_prod.retrieval.models import RetrievalLimits


SCHEMA_VERSION = "industrial-retrieval-evaluation-v1"
SCHEMA_VERSION_V2 = "industrial-retrieval-evaluation-v2"
BASELINE_COMMIT = "feade8048d2dd34344fefd015b17b8ef43464f68"
VARIANTS = ("legacy_default", "scoped_default", "scoped_ranked")
RERANK_VARIANT = "scoped_reranked"
SUPPORTED_VARIANTS = (*VARIANTS, RERANK_VARIANT)
RERANK_USAGE_FIELDS = ("provider_calls", "cache_hits", "provider_reported_total_tokens",
    "provider_reported_prompt_tokens", "provider_reported_completion_tokens",
    "calls_without_reported_tokens", "calls_without_separate_token_usage")
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_VECTOR_CACHE_BYTES = 256 * 1024
MAX_DOCUMENTS = 128
MAX_CHUNKS = 2048
MAX_SOURCE_CHARS = 16 * 1024 * 1024
BASE_LIMITS = RetrievalLimits(
    top_k=5, seed_k=3, graph_entities_per_seed=8, graph_edges_per_seed=40,
    graph_candidates_per_seed=8, candidate_limit=50, anchor_k=3,
    minimum_vector_score=0.75,
)


class IndustrialEvaluationError(ValueError):
    """A capture, immutable input or evaluation boundary is invalid."""


def json_safe(value: Any) -> Any:
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(json_safe(item) for item in value)
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IndustrialEvaluationError("duplicate JSON object key")
        result[key] = value
    return result


def read_json(path: Path, *, max_bytes: int = MAX_REPORT_BYTES) -> Any:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise IndustrialEvaluationError("evaluation input must be a regular file")
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise IndustrialEvaluationError("evaluation input exceeds byte bound")
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(IndustrialEvaluationError("non-finite JSON number")))


def write_immutable_json(path: Path, payload: Any) -> None:
    encoded = (json.dumps(json_safe(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise IndustrialEvaluationError("evaluation output exceeds byte bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if read_json(path) != json_safe(payload):
            raise IndustrialEvaluationError("immutable evaluation artifact already differs")
        return
    fd, temporary = tempfile.mkstemp(prefix=".industrial-evaluation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_json(path) != json_safe(payload):
                raise IndustrialEvaluationError("concurrent immutable evaluation artifact differs") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def limits_for(variant: str) -> RetrievalLimits:
    if variant not in SUPPORTED_VARIANTS:
        raise IndustrialEvaluationError("unknown evaluation variant")
    return replace(BASE_LIMITS, anchor_k=BASE_LIMITS.top_k) if variant in {"scoped_ranked", RERANK_VARIANT} else BASE_LIMITS


def validate_vector(vector: Any, dimensions: int) -> tuple[float, ...]:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= 4096:
        raise IndustrialEvaluationError("invalid embedding dimensions")
    if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
        raise IndustrialEvaluationError("cached embedding dimension differs")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
        raise IndustrialEvaluationError("embedding vector contains invalid values")
    result = tuple(float(value) for value in vector)
    if not any(result):
        raise IndustrialEvaluationError("embedding vector must be nonzero")
    return result


def query_cache_identity(profile: Any, query: str) -> dict[str, Any]:
    values = json_safe(profile)
    if set(values) != {"provider", "model", "revision", "dimensions", "normalization"}:
        raise IndustrialEvaluationError("embedding cache profile is incomplete")
    return {"schema_version": "industrial-query-vector-v1", "profile": values,
            "query_checksum": content_checksum(query)}


def cached_query_vector(cache: Path, profile: Any, query: str, *, provider: Any = None) -> tuple[tuple[float, ...], bool]:
    """Reuse only exact model/query bindings; provider=None never fabricates data."""
    identity = query_cache_identity(profile, query)
    key = digest(identity)
    path = cache / f"{key}.json"
    if path.exists():
        payload = read_json(path, max_bytes=MAX_VECTOR_CACHE_BYTES)
        if not isinstance(payload, dict) or set(payload) != {"identity", "vector", "vector_checksum"} or payload["identity"] != identity:
            raise IndustrialEvaluationError("query vector cache identity differs")
        vector = validate_vector(payload["vector"], identity["profile"]["dimensions"])
        if payload["vector_checksum"] != digest(list(vector)):
            raise IndustrialEvaluationError("query vector cache checksum differs")
        return vector, False
    if provider is None:
        raise IndustrialEvaluationError("query vector cache is missing; explicit live provider required")
    vector = validate_vector(provider(query), identity["profile"]["dimensions"])
    write_immutable_json(path, {"identity": identity, "vector": list(vector), "vector_checksum": digest(list(vector))})
    return vector, True


def source_code_pin(root: Path) -> dict[str, Any]:
    """Hash actual source bytes as well as HEAD, including this round's new files."""
    root = root.resolve()
    paths = sorted((root / "src/graphrag_prod").rglob("*.py"))
    paths += [path for path in (
        root / "scripts/evaluate_industrial_retrieval.py",
        root / "scripts/industrial_legacy_retrieval_worker.py",
        root / "scripts/industrial_current_retrieval_worker.py",
    ) if path.is_file()]
    if not 1 <= len(paths) <= 512:
        raise IndustrialEvaluationError("source inventory exceeds bounds")
    hashes = {}
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise IndustrialEvaluationError("source pin requires bounded regular files")
        hashes[str(path.relative_to(root))] = content_checksum(path.read_bytes())
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    return {"commit": commit, "source_checksum": digest(hashes), "source_files": hashes}


def require_legacy_checkout(root: Path) -> dict[str, Any]:
    pin = source_code_pin(root)
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                           check=True, capture_output=True, text=True, timeout=10).stdout
    if pin["commit"] != BASELINE_COMMIT or dirty:
        raise IndustrialEvaluationError("legacy comparison requires the clean immutable I2 checkout")
    return pin


def corpus_bindings(corpus: Any) -> dict[str, dict[str, Any]]:
    """Reproduce loader Chunk identities from immutable authored source ranges."""
    bindings: dict[str, dict[str, Any]] = {}
    for document in corpus.documents:
        uri = f"industrial://{corpus.tenant_id}/{corpus.version}/{document.key}"
        document_identifier = document_id(corpus.tenant_id, uri)
        checksum = content_checksum(document.text)
        version_identifier = version_id(document_identifier, checksum, checksum)
        for ordinal, section in enumerate(document.sections):
            text = document.text[section.char_start:section.char_end]
            identifier = chunk_id(version_identifier, "industrial-authored-sections:v1", ordinal,
                                  section.char_start, section.char_end, content_checksum(text))
            anchor = f"{document.key}#{section.key}"
            if identifier in bindings:
                raise IndustrialEvaluationError("duplicate corpus Chunk identity")
            bindings[identifier] = {
                "anchor": anchor, "text": text, "checksum": content_checksum(text),
                "document_id": document_identifier, "version_id": version_identifier,
                "document_checksum": checksum, "char_start": section.char_start, "char_end": section.char_end,
                "tenant_id": corpus.tenant_id, "access_groups": list(document.access_groups),
                "family": document.family, "asset_keys": list(document.asset_keys),
                "source_kind": document.source_kind, "published_at": document.published_at,
            }
    return bindings


def capture_index_pins(driver: Any, database: str, tenants: Sequence[str]) -> list[dict[str, Any]]:
    rows, _, _ = driver.execute_query(
        "MATCH (state:TenantCorpusState) WHERE state.tenant_id IN $tenants "
        "OPTIONAL MATCH (state)-[:ACTIVE_EMBEDDING_INDEX]->(generation:EmbeddingIndexGeneration) "
        "OPTIONAL MATCH (publication_state:KnowledgePublicationState {tenant_id:state.tenant_id}) "
        "OPTIONAL MATCH (publication_state)-[:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication:KnowledgePublication) "
        "RETURN state.tenant_id AS tenant_id,state.corpus_revision AS corpus_revision,"
        "generation.generation_id AS generation_id,generation.embedding_space_id AS embedding_space_id,"
        "generation.dimensions AS dimensions,generation.state AS generation_state,"
        "generation.corpus_revision AS generation_corpus_revision,generation.tenant_id AS generation_tenant_id,"
        "publication.publication_id AS publication_id,publication.tenant_id AS publication_tenant_id,"
        "publication.status AS publication_status,"
        "coalesce(publication_state.activation_generation,0) AS knowledge_activation_generation "
        "ORDER BY tenant_id LIMIT 9", tenants=sorted(set(tenants)), database_=database,
    )
    result = [json_safe(dict(row)) for row in rows]
    if len(result) != len(set(tenants)) or {row["tenant_id"] for row in result} != set(tenants):
        raise IndustrialEvaluationError("every evaluation tenant needs one active index state")
    if any(row["generation_state"] != "ACTIVE" or row["corpus_revision"] != row["generation_corpus_revision"] for row in result):
        raise IndustrialEvaluationError("evaluation index state is not active and current")
    for row in result:
        if (row["generation_tenant_id"] != row["tenant_id"]
                or type(row["knowledge_activation_generation"]) is not int
                or row["knowledge_activation_generation"] < 0
                or (row["publication_id"] is not None and (
                    row["publication_status"] != "ACTIVE" or row["publication_tenant_id"] != row["tenant_id"]))):
            raise IndustrialEvaluationError("evaluation publication or index ownership is invalid")
    return result


def capture_source_snapshot(driver: Any, database: str, tenants: Sequence[str]) -> dict[str, Any]:
    """Privileged local audit snapshot, never an application recall endpoint.

    Captures all current source rows for the bounded test tenants so offline
    validation checks actual ACLs even for unexpected, non-gold retrieval hits.
    """
    rows, _, _ = driver.execute_query(
        "MATCH (d:Document)-[:ACTIVE_VERSION]->(v:DocumentVersion) "
        "WHERE d.tenant_id IN $tenants "
        "MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {build_state:'PUBLISHED'})-[:OF_VERSION]->(v) "
        "RETURN d.document_id AS document_id,v.version_id AS version_id,"
        "size(v.normalized_text) AS text_chars,size(coalesce(v.industrial_provenance_json,'')) AS map_chars "
        "ORDER BY document_id LIMIT $limit", tenants=sorted(set(tenants)), limit=MAX_DOCUMENTS + 1, database_=database,
    )
    if len(rows) > MAX_DOCUMENTS or sum(int(row["text_chars"] or 0) + int(row["map_chars"] or 0) for row in rows) > MAX_SOURCE_CHARS:
        raise IndustrialEvaluationError("current source snapshot exceeds audit bounds")
    identifiers = [row["version_id"] for row in rows]
    documents, _, _ = driver.execute_query(
        "MATCH (d:Document)-[:ACTIVE_VERSION]->(v:DocumentVersion) WHERE v.version_id IN $versions "
        "MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {build_state:'PUBLISHED'})-[:OF_VERSION]->(v) "
        "RETURN d { .document_id,.tenant_id,.canonical_uri,.source_name,.title,.access_policy_id,.access_policy_version,.access_groups} AS document,"
        "v { .version_id,.document_id,.tenant_id,.checksum,.original_checksum,.normalized_text,.version_number,.published_at,"
        ".industrial_provenance_json,.industrial_provenance_checksum,.industrial_family,.industrial_contract_family,"
        ".industrial_asset_keys,.industrial_source_kind,.industrial_source_key} AS version,"
        "s {.snapshot_id,.tenant_id,.document_id,.version_id,.build_state} AS snapshot "
        "ORDER BY document.document_id LIMIT $limit", versions=identifiers, limit=MAX_DOCUMENTS + 1, database_=database,
    )
    chunks, _, _ = driver.execute_query(
        "MATCH (d:Document)-[:ACTIVE_VERSION]->(v:DocumentVersion) WHERE v.version_id IN $versions "
        "MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {build_state:'PUBLISHED'})-[:OF_VERSION]->(v) "
        "MATCH (s)-[:INCLUDES_CHUNK]->(c:Chunk) "
        "RETURN c {.chunk_id,.document_id,.version_id,.tenant_id,.access_policy_id,.access_policy_version,.access_groups,"
        ".ordinal,.text,.checksum,.char_start,.char_end,.page_number,.section,.splitter_version} AS chunk "
        "ORDER BY chunk.chunk_id LIMIT $limit", versions=identifiers, limit=MAX_CHUNKS + 1, database_=database,
    )
    if len(documents) != len(identifiers) or len(chunks) > MAX_CHUNKS:
        raise IndustrialEvaluationError("current source snapshot changed or exceeds bounds")
    return {"documents": [json_safe(dict(row)) for row in documents],
            "chunks": [json_safe(dict(row["chunk"])) for row in chunks]}


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndustrialEvaluationError("source dates require explicit timezone")
    return parsed


def validate_source_snapshot(snapshot: Mapping[str, Any], corpus: Any) -> dict[str, dict[str, Any]]:
    """Check source text, exact ranges, immutable facets and current ACL parity."""
    if set(snapshot) != {"documents", "chunks"} or not isinstance(snapshot["documents"], list) or not isinstance(snapshot["chunks"], list):
        raise IndustrialEvaluationError("source snapshot shape is invalid")
    if len(snapshot["documents"]) > MAX_DOCUMENTS or len(snapshot["chunks"]) > MAX_CHUNKS:
        raise IndustrialEvaluationError("source snapshot exceeds bounds")
    versions: dict[str, dict[str, Any]] = {}
    total_chars = 0
    for entry in snapshot["documents"]:
        document, version, graph = (entry[key] for key in ("document", "version", "snapshot"))
        if (version["version_id"] in versions or document["tenant_id"] != version["tenant_id"]
                or graph["tenant_id"] != document["tenant_id"] or graph["document_id"] != document["document_id"]
                or graph["version_id"] != version["version_id"] or graph["build_state"] != "PUBLISHED"
                or document["document_id"] != version["document_id"]):
            raise IndustrialEvaluationError("source snapshot identity is inconsistent")
        text = version["normalized_text"]
        if not isinstance(text, str) or content_checksum(text) != version["checksum"]:
            raise IndustrialEvaluationError("source version text checksum differs")
        if type(version["version_number"]) is not int or version["version_number"] <= 0:
            raise IndustrialEvaluationError("source version number is invalid")
        if version_id(document["document_id"], version["checksum"], version["original_checksum"]) != version["version_id"]:
            raise IndustrialEvaluationError("source version identity differs from content")
        if document_id(document["tenant_id"], document["canonical_uri"]) != document["document_id"]:
            raise IndustrialEvaluationError("source document identity differs from URI")
        _timestamp(version.get("published_at"))
        total_chars += len(text)
        metadata = None
        encoded = version.get("industrial_provenance_json")
        if encoded is not None:
            if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 8 * 1024 * 1024 or content_checksum(encoded) != version.get("industrial_provenance_checksum"):
                raise IndustrialEvaluationError("source provenance checksum or bound differs")
            total_chars += len(encoded)
            metadata = json.loads(encoded, object_pairs_hook=_unique_object)
            from .provenance import _source_facets
            facets = _source_facets(metadata)
            if any(version.get(f"industrial_{key}") != value for key, value in facets.items()):
                raise IndustrialEvaluationError("source provenance scalar facets differ")
            for key, expected in {
                "tenant_id": document["tenant_id"], "document_id": document["document_id"],
                "version_id": version["version_id"], "normalized_checksum": version["checksum"],
                "original_checksum": version["original_checksum"], "access_policy_id": document["access_policy_id"],
                "access_policy_version": document["access_policy_version"], "access_groups": document["access_groups"],
            }.items():
                if metadata.get(key) != expected:
                    raise IndustrialEvaluationError("source provenance identity or current ACL differs")
            if metadata.get("source_locations"):
                from graphrag_prod.construction.parser import NormalizedSource, SourceLocation
                locations = tuple(SourceLocation(**{**row, "bbox": tuple(row["bbox"])}) for row in metadata["source_locations"])
                NormalizedSource(text, locations, metadata["parser_version"], tuple(metadata["selected_pages"]))
        elif any(version.get(f"industrial_{key}") is not None for key in ("family", "contract_family", "asset_keys", "source_kind", "source_key")):
            raise IndustrialEvaluationError("industrial facets have no immutable provenance")
        versions[version["version_id"]] = {"document": document, "version": version, "metadata": metadata}
    if total_chars > MAX_SOURCE_CHARS:
        raise IndustrialEvaluationError("source snapshot text exceeds bounds")
    result: dict[str, dict[str, Any]] = {}
    for chunk in snapshot["chunks"]:
        identifier = chunk["chunk_id"]
        if identifier in result or chunk["version_id"] not in versions:
            raise IndustrialEvaluationError("source Chunk is duplicated or lacks active version")
        parent = versions[chunk["version_id"]]
        document, version = parent["document"], parent["version"]
        for key in ("tenant_id", "document_id", "access_policy_id", "access_policy_version", "access_groups"):
            if chunk[key] != document[key]:
                raise IndustrialEvaluationError("source Chunk current identity or ACL differs")
        start, end = chunk["char_start"], chunk["char_end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(version["normalized_text"]):
            raise IndustrialEvaluationError("source Chunk range is invalid")
        if version["normalized_text"][start:end] != chunk["text"] or content_checksum(chunk["text"]) != chunk["checksum"]:
            raise IndustrialEvaluationError("source Chunk exact text or checksum differs")
        if type(chunk["ordinal"]) is not int or chunk["ordinal"] < 0:
            raise IndustrialEvaluationError("source Chunk ordinal is invalid")
        if chunk_id(chunk["version_id"], chunk["splitter_version"], chunk["ordinal"], start, end, chunk["checksum"]) != identifier:
            raise IndustrialEvaluationError("source Chunk identity differs from exact source range")
        if parent["metadata"] and parent["metadata"].get("source_locations"):
            pages = [row for row in parent["metadata"]["source_locations"] if row["kind"] == "page"
                     and row["char_start"] <= start < end <= row["char_end"]]
            if len(pages) != 1 or chunk.get("page_number") != pages[0]["page_number"]:
                raise IndustrialEvaluationError("PDF Chunk crosses or misidentifies a physical page")
        result[identifier] = {**parent, "chunk": chunk}
    expected = corpus_bindings(corpus)
    if not set(expected) <= set(result):
        raise IndustrialEvaluationError("current source snapshot omits pinned authored corpus chunks")
    for identifier, binding in expected.items():
        item = result[identifier]
        if (item["chunk"]["text"] != binding["text"] or item["version"]["checksum"] != binding["document_checksum"]
                or item["document"]["access_groups"] != binding["access_groups"]
                or item["metadata"] is None or item["metadata"]["family"] != binding["family"]
                or item["metadata"]["asset_keys"] != binding["asset_keys"]):
            raise IndustrialEvaluationError("current source differs from frozen corpus content or scope")
    return result


def _scope_violations(case: Any, item: Mapping[str, Any]) -> list[str]:
    metadata = item["metadata"] or {}
    scope = case.user_scope
    failures = []
    if metadata.get("family") != scope.family:
        failures.append("product_family")
    if getattr(scope, "source_kinds", ()) and metadata.get("source_kind") not in scope.source_kinds:
        failures.append("source_kind")
    assets = set(metadata.get("asset_keys", ()))
    reference = metadata.get("source_kind") in {"CURATED_REFERENCE", "OFFICIAL_PUBLICATION"}
    if reference and not scope.include_family_references:
        failures.append("reference_excluded")
    if scope.asset_keys and not (assets & set(scope.asset_keys)) and not (reference and scope.include_family_references):
        failures.append("primary_asset")
    cutoff = _timestamp(scope.published_at_lte)
    date = _timestamp(item["version"].get("published_at"))
    if cutoff is not None and (date is None or date > cutoff):
        failures.append("publication_cutoff")
    return failures


def _visible_ids(raw: Mapping[str, Any]) -> set[str]:
    trace = raw.get("trace") or {}
    identifiers = {chunk["citation"]["chunk_id"] for chunk in raw["chunks"]}
    for stage in ("vector_recall", "bm25_recall", "seed_ranking", "graph_expansion", "candidate_vector_ranking", "final_ranking", "decisions"):
        for row in trace.get(stage, []):
            identifiers.add(row["chunk_id"])
    identifiers.update(trace.get("selected_chunk_ids", []))
    reranking = trace.get("reranking") or {}
    identifiers.update(reranking.get("candidate_chunk_ids", []))
    if reranking.get("response"):
        identifiers.update(row["chunk_id"] for row in reranking["response"]["scores"])
    for event in raw.get("rerank_cache_events", []):
        identifiers.update(row["chunk_id"] for row in event["identity"]["candidates"])
    if len(identifiers) > MAX_CHUNKS:
        raise IndustrialEvaluationError("retrieval trace exceeds audit bounds")
    return identifiers


def _audit_reranking(case: Any, raw: Mapping[str, Any], sources: Mapping[str, Any], pins: Mapping[str, Any]) -> None:
    if raw["variant"] != RERANK_VARIANT or raw.get("error") is not None:
        return
    from graphrag_prod.retrieval.reranking import RerankCandidate, RerankResponse, RerankScore, RerankTrace
    from graphrag_prod.retrieval.rerank_provider import provider_configuration, rerank_cache_identity, validate_cached_response
    configuration = pins.get("reranker")
    if (not configuration or configuration != provider_configuration(profile=configuration["profile"])
            or raw.get("reranker") != configuration):
        raise IndustrialEvaluationError("reranking provider configuration differs from run pins")
    usage = raw.get("rerank_usage")
    expected_usage_keys = set(RERANK_USAGE_FIELDS)
    if not isinstance(usage, dict) or set(usage) != expected_usage_keys or any(type(item) is not int or item < 0 for item in usage.values()):
        raise IndustrialEvaluationError("reranking usage metadata is invalid")
    trace = raw["trace"].get("reranking")
    events = raw.get("rerank_cache_events")
    if trace is None or trace["status"] == "SKIPPED_EMPTY":
        if any(usage.values()) or events != [] or raw["chunks"]:
            raise IndustrialEvaluationError("empty reranking cannot carry provider results")
        return
    response_fields = dict(trace["response"])
    response_fields["scores"] = tuple(RerankScore(**item) for item in response_fields["scores"])
    for field, value in tuple(response_fields.items()):
        if field != "scores" and isinstance(value, list):
            response_fields[field] = tuple(value)
    response = RerankResponse(**response_fields)
    trace_fields = dict(trace)
    trace_fields["response"] = response
    for field in ("candidate_chunk_ids", "ranked_chunk_ids"):
        if field in trace_fields:
            trace_fields[field] = tuple(trace_fields[field])
    RerankTrace(**trace_fields)
    candidates = []
    for identifier in trace["candidate_chunk_ids"]:
        source = sources.get(identifier)
        if source is None:
            raise IndustrialEvaluationError("reranking candidate is outside the exact source snapshot")
        chunk, document, version = source["chunk"], source["document"], source["version"]
        candidates.append(RerankCandidate(identifier, chunk["text"], chunk["checksum"],
            source_title=document["title"], source_section=chunk.get("section"),
            document_id=document["document_id"], version_id=version["version_id"]))
    candidates = tuple(candidates)
    context = rerank_request_context(case.user_scope.asset_keys, configuration)
    if raw.get("rerank_context") != context:
        raise IndustrialEvaluationError("reranking query context differs from explicit user scope")
    provider_query = case.question + "\n" + context if context else case.question
    provider_raw = raw.get("rerank_provider_raw")
    validate_cached_response(provider_query, candidates, response, profile=configuration["profile"], raw_response_json=provider_raw)
    identity = rerank_cache_identity(provider_query, candidates, profile=configuration["profile"])
    cache_payload = {"identity": identity, "response": json_safe(response)}
    if provider_raw is not None:
        if not isinstance(provider_raw, str) or len(provider_raw.encode("utf-8")) > MAX_VECTOR_CACHE_BYTES:
            raise IndustrialEvaluationError("private provider response exceeds capture bounds")
        cache_payload["raw_response_json"] = provider_raw
    if (not isinstance(events, list) or len(events) != 1 or events[0]["identity"] != identity
            or events[0]["cache_key"] != digest(identity) or type(events[0]["cache_hit"]) is not bool
            or events[0]["cache_checksum"] != digest(cache_payload)):
        raise IndustrialEvaluationError("reranking cache evidence differs from actual exact inputs")
    hit = events[0]["cache_hit"]
    expected_usage = {key: 0 for key in RERANK_USAGE_FIELDS}
    expected_usage.update(provider_calls=int(not hit), cache_hits=int(hit))
    if not hit:
        if type(response.total_tokens) is int:
            expected_usage["provider_reported_total_tokens"] = response.total_tokens
        else:
            expected_usage["calls_without_reported_tokens"] = 1
        if type(response.prompt_tokens) is int and type(response.completion_tokens) is int:
            expected_usage["provider_reported_prompt_tokens"] = response.prompt_tokens
            expected_usage["provider_reported_completion_tokens"] = response.completion_tokens
        else:
            expected_usage["calls_without_separate_token_usage"] = 1
    if usage != expected_usage:
        raise IndustrialEvaluationError("reranking cache reuse was mislabeled as a live provider call")


def _audit_case(case: Any, raw: Mapping[str, Any], sources: Mapping[str, Any], bindings: Mapping[str, Any], pins: Mapping[str, Any]) -> dict[str, Any]:
    variant = raw["variant"]
    if raw["query_checksum"] != content_checksum(case.question):
        raise IndustrialEvaluationError("retrieval capture query differs from gold")
    limits = limits_for(variant)
    if raw.get("error") is not None and raw["chunks"]:
        raise IndustrialEvaluationError("failed retrieval capture cannot include successful chunks")
    chunks = raw["chunks"]
    if not isinstance(chunks, list) or len(chunks) > limits.top_k:
        raise IndustrialEvaluationError("retrieved context exceeds item bound")
    ranking = [row["citation"]["chunk_id"] for row in chunks]
    if len(ranking) != len(set(ranking)):
        raise IndustrialEvaluationError("retrieval context contains duplicate Chunk IDs")
    trace = raw.get("trace")
    if raw.get("error") is None:
        source_pin = pins["legacy_source"] if variant == "legacy_default" else pins["current_source"]
        if raw.get("implementation_checksum") != source_pin["source_files"]["src/graphrag_prod/retrieval/engine.py"]:
            raise IndustrialEvaluationError("retrieval implementation differs from source pin")
        if not isinstance(trace, dict) or trace["selected_chunk_ids"] != ranking or trace["limits"] != json_safe(limits):
            raise IndustrialEvaluationError("retrieval trace selection or limits differ")
        expected_index = next((index for index in pins["indexes"] if index["tenant_id"] == case.principal.tenant_id), None)
        if expected_index is None or any(trace[field] != expected_index[key] for field, key in (
            ("tenant_id", "tenant_id"), ("corpus_revision", "corpus_revision"),
            ("embedding_generation_id", "generation_id"), ("embedding_space_id", "embedding_space_id"),
        )):
            raise IndustrialEvaluationError("retrieval ran against a different index generation")
        # Original v1 captures did not pin this field. Keep their measured
        # baseline valid without attributing the stronger ABA check to them.
        if variant != "legacy_default" and "knowledge_activation_generation" in expected_index:
            if (trace.get("knowledge_publication_id") != expected_index["publication_id"]
                    or trace.get("knowledge_activation_generation") != expected_index["knowledge_activation_generation"]):
                raise IndustrialEvaluationError("retrieval ran against a different publication activation")
    _audit_reranking(case, raw, sources, pins)
    visible = _visible_ids(raw)
    exposures, scope_errors, citation_errors = [], [], []
    for identifier in sorted(visible):
        item = sources.get(identifier)
        if item is None:
            exposures.append({"chunk_id": identifier, "reason": "not_in_current_source_snapshot"})
            continue
        document, chunk = item["document"], item["chunk"]
        if (document["tenant_id"] != case.principal.tenant_id or chunk["tenant_id"] != case.principal.tenant_id
                or not set(case.principal.access_groups) & set(document["access_groups"])
                or not set(case.principal.access_groups) & set(chunk["access_groups"])):
            exposures.append({"chunk_id": identifier, "reason": "tenant_or_group_denied"})
        for reason in _scope_violations(case, item):
            scope_errors.append({"chunk_id": identifier, "reason": reason})
    for returned in chunks:
        citation = returned["citation"]
        item = sources.get(citation["chunk_id"])
        if item is None:
            citation_errors.append({"chunk_id": citation["chunk_id"], "field": "current_source"})
            continue
        chunk, version, document = item["chunk"], item["version"], item["document"]
        expected = {"chunk_id": chunk["chunk_id"], "chunk_checksum": chunk["checksum"],
            "document_id": document["document_id"], "canonical_uri": document["canonical_uri"],
            "source_name": document["source_name"], "version_id": version["version_id"],
            "version_checksum": version["checksum"], "version_number": version["version_number"],
            "ordinal": chunk["ordinal"], "char_start": chunk["char_start"], "char_end": chunk["char_end"],
            "page_number": chunk.get("page_number"), "section": chunk.get("section"),
            "document_title": document["title"], "published_at": version.get("published_at")}
        for field, value in expected.items():
            actual = citation.get(field)
            equal = _timestamp(actual) == _timestamp(value) if field == "published_at" else actual == value
            if field in {"version_number", "ordinal", "char_start", "char_end", "page_number"} and value is not None:
                equal = equal and type(actual) is int
            if not equal:
                citation_errors.append({"chunk_id": chunk["chunk_id"], "field": field})
        if returned["text"] != chunk["text"]:
            citation_errors.append({"chunk_id": chunk["chunk_id"], "field": "exact_text"})
    context_chars = sum(len(row["text"]) for row in chunks)
    if context_chars > limits.max_context_chars or (trace is not None and trace["context_chars"] != context_chars):
        raise IndustrialEvaluationError("retrieval context character budget differs")
    anchor_ranking = [bindings[identifier]["anchor"] if identifier in bindings else f"unjudged:{identifier}" for identifier in ranking]
    positive = bool(case.relevance)
    metrics = None
    if positive:
        calculated = evaluate_retrieval_items([{"id": case.case_id, "answerable": True, "relevance": case.relevance,
            "ranking": anchor_ranking, "unauthorized_exposures": []}])
        metrics = {"recall_at_5": calculated.recall_at_5, "mrr": calculated.mrr, "ndcg_at_5": calculated.ndcg_at_5}
    complete = any(set(alternative) <= set(anchor_ranking) for alternative in case.required_evidence_sets) if positive else None
    forbidden = {anchor.key for anchor in case.forbidden_sections}
    forbidden_visible = [identifier for identifier in visible if identifier in bindings and bindings[identifier]["anchor"] in forbidden]
    return {"case_id": case.case_id, "split": case.split, "question_class": case.question_class,
        "answerability_annotation": case.answerability, "positive_target": positive,
        "ranking": anchor_ranking, "metrics": metrics, "complete_evidence_set_covered": complete,
        "current_acl_exposures": exposures, "scope_exclusions_violated": scope_errors,
        "citation_errors": citation_errors, "forbidden_sentinel_exposures": sorted(forbidden_visible),
        "runtime_error": raw.get("error"), "context_chars": context_chars,
        "duration_ms": raw["duration_ms"]}


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    positive = [row for row in rows if row["positive_target"]]
    return {"case_count": len(rows), "positive_target_count": len(positive),
        "answerability_counts": {label: sum(row["answerability_annotation"] == label for row in rows)
                                  for label in ("SUPPORTED", "INSUFFICIENT", "AMBIGUOUS", "DENIED")},
        **{name: math.fsum(row["metrics"][name] for row in positive) / len(positive) if positive else None
           for name in ("recall_at_5", "mrr", "ndcg_at_5")},
        "complete_evidence_set_rate": sum(row["complete_evidence_set_covered"] for row in positive) / len(positive) if positive else None,
        "current_acl_exposure_count": sum(len(row["current_acl_exposures"]) for row in rows),
        "scope_violation_count": sum(len(row["scope_exclusions_violated"]) for row in rows),
        "citation_error_count": sum(len(row["citation_errors"]) for row in rows),
        "forbidden_sentinel_exposure_count": sum(len(row["forbidden_sentinel_exposures"]) for row in rows),
        "runtime_error_count": sum(row["runtime_error"] is not None for row in rows)}


def evaluate_captures(gold: Any, corpus: Any, captures: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any], pins: Mapping[str, Any], *, additional_bindings: Mapping[str, Any] | None = None, variants: tuple[str, ...] = VARIANTS) -> dict[str, Any]:
    """Score every declared case; omission of even one negative is an error."""
    if gold.corpus_checksum != corpus.manifest_checksum or pins["corpus_checksum"] != corpus.manifest_checksum:
        raise IndustrialEvaluationError("gold or capture corpus pin differs")
    if pins["gold_checksum"] != gold.checksum or pins["gold_version"] != gold.version:
        raise IndustrialEvaluationError("capture gold pin differs")
    if pins["source_snapshot_checksum"] != digest(snapshot):
        raise IndustrialEvaluationError("captured source snapshot checksum differs")
    if pins["legacy_source"]["commit"] != BASELINE_COMMIT:
        raise IndustrialEvaluationError("legacy source commit differs")
    for key in ("legacy_source", "current_source"):
        pin = pins[key]
        if not pin["source_files"] or pin["source_checksum"] != digest(pin["source_files"]):
            raise IndustrialEvaluationError("source-code pin checksum differs")
    if not variants or len(set(variants)) != len(variants) or any(item not in SUPPORTED_VARIANTS for item in variants):
        raise IndustrialEvaluationError("declared evaluation variants are invalid")
    if pins["configurations"] != {variant: json_safe(limits_for(variant)) for variant in variants}:
        raise IndustrialEvaluationError("predeclared retrieval configuration differs")
    sources = validate_source_snapshot(snapshot, corpus)
    bindings = corpus_bindings(corpus)
    if additional_bindings:
        if set(bindings) & set(additional_bindings):
            raise IndustrialEvaluationError("additional evidence identities overlap authored corpus")
        bindings.update(additional_bindings)
    cases = {case.case_id: case for case in gold.cases}
    if len(cases) != len(gold.cases) or not cases:
        raise IndustrialEvaluationError("gold case identity is invalid")
    actual: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in captures:
        key = (raw["variant"], raw["case_id"])
        if key in actual or key[0] not in variants or key[1] not in cases:
            raise IndustrialEvaluationError("retrieval captures contain duplicate or unknown cases")
        actual[key] = raw
    if set(actual) != {(variant, identifier) for variant in variants for identifier in cases}:
        raise IndustrialEvaluationError("retrieval capture coverage omits cases or negatives")
    for case in gold.cases:
        checksums = {actual[(variant, case.case_id)]["query_vector_checksum"] for variant in variants}
        if len(checksums) != 1 or not next(iter(checksums)) or pins["query_vectors"][case.case_id] != next(iter(checksums)):
            raise IndustrialEvaluationError("comparison variants used different query vectors")
    output = {}
    for variant in variants:
        rows = [_audit_case(case, actual[(variant, case.case_id)], sources, bindings, pins) for case in gold.cases]
        output[variant] = {"cases": rows, "all": _aggregate(rows),
            "dev": _aggregate([row for row in rows if row["split"] == "dev"]),
            "holdout": _aggregate([row for row in rows if row["split"] == "holdout"])}
    return {"variants": output,
        "runtime_answer_and_refusal_evaluated": False,
        "annotation_note": "SUPPORTED/INSUFFICIENT/AMBIGUOUS/DENIED are source gold labels, not runtime answer decisions.",
        "denial_note": "Authorized related summaries are permitted; every exposed Chunk is checked against actual current ACLs.",
        "metric_note": "Standard fractional Recall@5, MRR and nDCG include every positive-target case; complete alternative-set coverage is separate.",
        "official_pdf_integration": {"status": "NOT_RUN", "reason": "Separate pinned PDF integration capture is required; authored gold does not qualify it."},
        "graph_ui_validation": "NOT_RUN_I4", "production_candidate_eligible": False}


def pdf_evaluation_inputs(manifest: Mapping[str, Any], corpus: Any, snapshot: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Bind independent PDF annotations to exact stored original/page/range pins."""
    if manifest.get("schema_version") != "industrial-pdf-integration-gold-v1" or manifest.get("included_in_primary_gold_metrics") is not False:
        raise IndustrialEvaluationError("PDF integration manifest shape differs")
    if digest({key: value for key, value in manifest.items() if key != "checksum"}) != manifest.get("checksum"):
        raise IndustrialEvaluationError("PDF integration manifest checksum differs")
    sources = validate_source_snapshot(snapshot, corpus)
    bindings, cases = {}, []
    if not 1 <= len(manifest["cases"]) <= 8:
        raise IndustrialEvaluationError("PDF integration case count exceeds bounds")
    for raw in manifest["cases"]:
        selected = {identifier: item for identifier, item in sources.items()
                    if (item["metadata"] or {}).get("source_kind") == "OFFICIAL_PUBLICATION"
                    and item["metadata"].get("source", {}).get("source_id") == raw["source_id"]}
        if not selected:
            raise IndustrialEvaluationError("required official PDF excerpt is not loaded")
        versions = {item["version"]["version_id"] for item in selected.values()}
        if len(versions) != 1:
            raise IndustrialEvaluationError("PDF evidence has multiple active derivative versions")
        by_ordinal = {}
        for identifier, item in selected.items():
            metadata, version, chunk = item["metadata"], item["version"], item["chunk"]
            if (version["original_checksum"] != raw["original_checksum"] or version["checksum"] != raw["normalized_checksum"]
                    or metadata["artifact_checksum"] != raw["artifact_checksum"] or metadata["parser_version"] != raw["parser_version"]
                    or chunk["splitter_version"] != raw["splitter_signature"] or metadata["family"] != raw["family"]
                    or metadata["source"].get("embedded_revision") != raw["embedded_revision"]):
                raise IndustrialEvaluationError("PDF source edition, normalization or splitter pin differs")
            anchor = f"{raw['source_id']}#ordinal-{chunk['ordinal']}"
            bindings[identifier] = {"anchor": anchor}
            if chunk["ordinal"] in by_ordinal:
                raise IndustrialEvaluationError("PDF source ordinal is duplicated")
            by_ordinal[chunk["ordinal"]] = chunk
        relevant = {}
        for required in raw["required_chunks"]:
            if any(type(required[key]) is not int for key in ("ordinal", "char_start", "char_end", "page_number")):
                raise IndustrialEvaluationError("PDF evidence coordinates require exact integers")
            ordinal = required["ordinal"]
            chunk = by_ordinal.get(ordinal)
            if chunk is None or any(chunk.get(key) != required[key] for key in ("char_start", "char_end", "page_number")) or chunk["checksum"] != required["text_checksum"]:
                raise IndustrialEvaluationError("PDF annotation does not match exact page/range/text")
            if required["page_number"] not in raw["physical_pages"]:
                raise IndustrialEvaluationError("PDF annotation page is outside declared question evidence")
            relevant[f"{raw['source_id']}#ordinal-{ordinal}"] = 3
        alternatives = tuple(tuple(f"{raw['source_id']}#ordinal-{ordinal}" for ordinal in group)
                             for group in raw["required_evidence_sets"])
        if not relevant or not alternatives or any(not group or not set(group) <= set(relevant) for group in alternatives):
            raise IndustrialEvaluationError("PDF complete evidence alternatives are invalid")
        cases.append(SimpleNamespace(case_id=raw["case_id"], question=raw["question"], split="integration",
            question_class="pdf_context", principal=SimpleNamespace(tenant_id=corpus.tenant_id, access_groups=("public",)),
            user_scope=SimpleNamespace(family=raw["family"], asset_keys=(), include_family_references=True,
                                      source_kinds=("OFFICIAL_PUBLICATION",), published_at_lte=None, origin="USER_SUPPLIED"),
            answerability=raw["answerability"], relevance=relevant, required_evidence_sets=alternatives, forbidden_sections=()))
    return SimpleNamespace(cases=tuple(cases), version=manifest["version"], checksum=manifest["checksum"],
                           corpus_checksum=corpus.manifest_checksum), bindings


def _pdf_result(suite: Mapping[str, Any], corpus: Any, snapshot: Mapping[str, Any], pins: Mapping[str, Any], *, expected_manifest: Mapping[str, Any] | None = None, variants: tuple[str, ...] = VARIANTS) -> dict[str, Any]:
    if suite["status"] == "NOT_RUN":
        if set(suite) != {"status", "reason"}:
            raise IndustrialEvaluationError("unrun PDF suite cannot carry successful capture data")
        return dict(suite)
    if suite["status"] != "RAN" or set(suite) != {"status", "manifest", "captures", "query_vectors", "original_cache_verification"}:
        raise IndustrialEvaluationError("PDF suite capture shape differs")
    if expected_manifest is None:
        from .gold import load_pdf_gold
        expected_manifest = load_pdf_gold()
    if suite["manifest"] != expected_manifest:
        raise IndustrialEvaluationError("PDF capture annotations differ from frozen manifest")
    gold, bindings = pdf_evaluation_inputs(suite["manifest"], corpus, snapshot)
    expected_originals = {item["source_id"]: item["original_checksum"] for item in suite["manifest"]["cases"]}
    if suite["original_cache_verification"] != expected_originals:
        raise IndustrialEvaluationError("PDF capture lacks original-cache verification pins")
    pdf_pins = {**pins, "gold_checksum": gold.checksum, "gold_version": gold.version, "query_vectors": suite["query_vectors"]}
    result = evaluate_captures(gold, corpus, suite["captures"], snapshot, pdf_pins, additional_bindings=bindings, variants=variants)
    return {"status": "RAN", "manifest_checksum": gold.checksum, "included_in_primary_gold_metrics": False,
            "variants": result["variants"], "runtime_answer_and_refusal_evaluated": False,
            "note": "Measures exact original/page/table context coverage, not a site procedure or hardware diagnosis."}


def select_capture_gold(gold: Any, capture_scope: Mapping[str, Any]) -> Any:
    """Select the entire declared split, never caller-specified case IDs."""
    if (set(capture_scope) != {"split", "variants"} or capture_scope["split"] not in {"dev", "all"}
            or capture_scope["variants"] != [RERANK_VARIANT]):
        raise IndustrialEvaluationError("v2 requires the declared complete dev or all rerank capture")
    cases = tuple(case for case in gold.cases if capture_scope["split"] == "all" or case.split == "dev")
    if not cases or (len(gold.cases) == 72 and len(cases) != (36 if capture_scope["split"] == "dev" else 72)):
        raise IndustrialEvaluationError("declared gold split is incomplete")
    return SimpleNamespace(cases=cases, version=gold.version, checksum=gold.checksum, corpus_checksum=gold.corpus_checksum)


def _evaluate_report_scope(gold: Any, corpus: Any, captures: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any],
                           pins: Mapping[str, Any], pdf_suite: Mapping[str, Any], capture_scope: Mapping[str, Any] | None) -> dict[str, Any]:
    variants = VARIANTS
    if capture_scope is not None:
        gold = select_capture_gold(gold, capture_scope)
        variants = tuple(capture_scope["variants"])
        if capture_scope["split"] == "dev" and pdf_suite["status"] != "NOT_RUN":
            raise IndustrialEvaluationError("dev-only capture cannot include PDF predictions")
        all_rows = [*captures, *pdf_suite.get("captures", [])]
        expected_usage = {name: sum(row.get("rerank_usage", {}).get(name, 0) for row in all_rows) for name in RERANK_USAGE_FIELDS}
        expected_usage["estimated_cost_usd"] = None
        if pins.get("rerank_usage") != expected_usage:
            raise IndustrialEvaluationError("reranking aggregate usage differs from actual captured calls")
    evaluation = evaluate_captures(gold, corpus, captures, snapshot, pins, variants=variants)
    evaluation["official_pdf_integration"] = _pdf_result(pdf_suite, corpus, snapshot, pins, variants=variants)
    if capture_scope is not None:
        evaluation["capture_scope"] = dict(capture_scope)
        evaluation["capture_completeness"] = "PARTIAL_DEV_ONLY" if capture_scope["split"] == "dev" else "COMPLETE_CORE_GOLD"
    return evaluation


def build_report(gold: Any, corpus: Any, captures: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any], pins: Mapping[str, Any], *, pdf_suite: Mapping[str, Any] | None = None, capture_scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    pdf_suite = pdf_suite or {"status": "NOT_RUN", "reason": "--include-pdf was not requested; no PDF integration pass is claimed."}
    evaluation = _evaluate_report_scope(gold, corpus, captures, snapshot, pins, pdf_suite, capture_scope)
    result = {"schema_version": SCHEMA_VERSION_V2 if capture_scope is not None else SCHEMA_VERSION,
              "pins": json_safe(pins), "source_snapshot": json_safe(snapshot),
              "captures": json_safe(captures), "pdf_suite": json_safe(pdf_suite), "evaluation": evaluation}
    if capture_scope is not None:
        result["capture_scope"] = json_safe(capture_scope)
    return {**result, "report_checksum": digest(result)}


def validate_report(report: Mapping[str, Any], gold: Any, corpus: Any, *, expected_pins: Mapping[str, Any] | None = None) -> dict[str, Any]:
    expected_keys = {"schema_version", "pins", "source_snapshot", "captures", "pdf_suite", "evaluation", "report_checksum"}
    if report["schema_version"] == SCHEMA_VERSION_V2:
        expected_keys.add("capture_scope")
    if set(report) != expected_keys or report["schema_version"] not in {SCHEMA_VERSION, SCHEMA_VERSION_V2}:
        raise IndustrialEvaluationError("industrial evaluation report shape differs")
    if expected_pins is not None and report["pins"] != expected_pins:
        raise IndustrialEvaluationError("report differs from independently supplied run pins")
    body = {key: value for key, value in report.items() if key != "report_checksum"}
    if digest(body) != report["report_checksum"]:
        raise IndustrialEvaluationError("evaluation report checksum differs")
    computed = _evaluate_report_scope(gold, corpus, report["captures"], report["source_snapshot"], report["pins"], report["pdf_suite"], report.get("capture_scope"))
    if computed != report["evaluation"]:
        raise IndustrialEvaluationError("reported metrics differ from raw retrieval captures")
    return computed


def assess_acceptance(evaluation: Mapping[str, Any], contract: Mapping[str, Any], *,
                      selected_variant: str, split: str, report_checksum: str) -> dict[str, Any]:
    """Assess one explicitly selected configuration without exposing other splits.

    This is an I3 retrieval assessment. The contract's graph browsing bound
    requirement remains pending I4 and cannot be qualified by retrieval scores.
    """
    from .contract import validate_industrial_contract
    validate_industrial_contract(contract)
    if selected_variant not in SUPPORTED_VARIANTS or selected_variant not in evaluation["variants"] or split not in {"dev", "holdout", "all"}:
        raise IndustrialEvaluationError("acceptance variant or split is invalid")
    if evaluation.get("capture_scope", {}).get("split") == "dev" and split != "dev":
        raise IndustrialEvaluationError("dev-only capture cannot qualify holdout or the full suite")
    total = evaluation["variants"][selected_variant][split]
    targets = {item["id"]: item for item in contract["evaluation"]["metrics"]}
    checks = []
    for name in ("recall_at_5", "mrr", "ndcg_at_5"):
        target, actual = targets[name], total[name]
        passed = (type(actual) in {int, float} and math.isfinite(actual)
                  and (actual >= target["target"] if target["operator"] == "gte" else actual == target["target"]))
        checks.append({"metric": name, "operator": target["operator"], "target": target["target"],
                       "actual": actual, "passed": passed})
    for metric, field in (("product_scope_violations", "scope_violation_count"),
                          ("authorization_violations", "current_acl_exposure_count")):
        checks.append({"metric": metric, "operator": "eq", "target": 0,
                       "actual": total[field], "passed": total[field] == 0})
    # The existing aggregate counts invalid citation fields, rather than
    # invalid citations. Do not invent a ratio with that denominator.
    for name in ("citation_error_count", "forbidden_sentinel_exposure_count", "runtime_error_count"):
        checks.append({"metric": name, "operator": "eq", "target": 0,
                       "actual": total[name], "passed": total[name] == 0})
    result = {"schema_version": "industrial-retrieval-acceptance-v1", "report_checksum": report_checksum,
        "contract_checksum": digest(contract), "selected_variant": selected_variant, "split": split,
        "case_count": total["case_count"], "positive_target_count": total["positive_target_count"],
        "status": "PASSED" if all(item["passed"] for item in checks) else "FAILED", "checks": checks,
        "coverage": "I3_RETRIEVAL_ONLY", "graph_bound_validation": "NOT_RUN_I4",
        "runtime_answer_and_refusal_evaluated": False, "production_candidate_eligible": False,
        "holdout_acceptance_assessed": split in {"holdout", "all"}}
    if split == "all":
        result["split_assessments"] = {part: assess_acceptance(evaluation, contract,
            selected_variant=selected_variant, split=part, report_checksum=report_checksum) for part in ("dev", "holdout")}
        result["acceptance_population"] = "Complete primary gold; split assessments remain explicit diagnostic results."
    return {**result, "assessment_checksum": digest(result)}


def rerank_request_context(asset_keys: Sequence[str], configuration: Mapping[str, Any]) -> str | None:
    version = configuration.get("query_context_version")
    if version is None:
        return None
    if version != "user-selected-equipment:v1":
        raise IndustrialEvaluationError("unknown reranking user-scope context version")
    from .retrieval import build_rerank_scope_context
    return build_rerank_scope_context(tuple(asset_keys))


def prepare_requests(cases: Sequence[Any], profile: Any, vectors: Mapping[str, Sequence[float]], *,
                     variant: str, driver: Any = None, database: str = "neo4j",
                     reranker_configuration: Mapping[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prepare only public request fields; never copy relevance or answer labels."""
    from graphrag_prod.domain.access import Principal
    from graphrag_prod.retrieval.models import VersionFilter
    requests, traces = [], {}
    for case in cases:
        principal = Principal("industrial-evaluation-reader", case.principal.tenant_id, frozenset(case.principal.access_groups))
        cutoff = _timestamp(case.user_scope.published_at_lte)
        if variant == "legacy_default":
            filters = {"document_ids": [], "version_ids": [],
                       "published_at_or_before": cutoff.isoformat() if cutoff else None}
            traces[case.case_id] = {"scope_capability": "UNSUPPORTED_BY_BASELINE"}
        else:
            from .retrieval import IndustrialScope, Neo4jIndustrialScopeResolver
            if driver is None:
                raise IndustrialEvaluationError("scoped capture requires a current authorized scope resolution")
            resolution = Neo4jIndustrialScopeResolver(driver, database).resolve(
                principal, IndustrialScope(family=case.user_scope.family,
                    asset_keys=case.user_scope.asset_keys, include_references=case.user_scope.include_family_references,
                    source_kinds=getattr(case.user_scope, "source_kinds", ())),
                version_filter=VersionFilter(published_at_or_before=cutoff),
            )
            filters = json_safe(resolution.version_filter)
            traces[case.case_id] = resolution.trace.as_dict()
        vector = validate_vector(vectors[case.case_id], profile.dimensions)
        requests.append({"case_id": case.case_id, "variant": variant, "query": case.question,
            "query_checksum": content_checksum(case.question), "query_vector_checksum": digest(list(vector)),
            "vector": list(vector), "embedding_space_id": profile.embedding_space_id,
            "principal": {"principal_id": principal.principal_id, "tenant_id": principal.tenant_id, "groups": sorted(principal.groups)},
            "limits": json_safe(limits_for(variant)), "version_filter": filters})
        if variant == RERANK_VARIANT:
            if not reranker_configuration:
                raise IndustrialEvaluationError("reranking request requires a declared provider profile")
            requests[-1]["rerank_context"] = rerank_request_context(case.user_scope.asset_keys, reranker_configuration)
    return requests, traces


def run_retrieval_worker(requests: Sequence[Mapping[str, Any]], *, source_root: Path,
                         worker_path: Path, progress: Any = None, deadline_seconds: float = 60.0,
                         worker_arguments: tuple[str, ...] = (), allow_provider_credentials: bool = False) -> list[dict[str, Any]]:
    """One isolated worker, bounded per-case waits and sanitized failure capture."""
    if not 0 < deadline_seconds <= 60 or not 1 <= len(requests) <= 128:
        raise IndustrialEvaluationError("worker request count or deadline exceeds bounds")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root.resolve() / "src")
    if not allow_provider_credentials:
        for key in ("OPENAI_API_KEY", "DASHSCOPE_API_KEY"):
            environment.pop(key, None)
    captures = []
    with tempfile.TemporaryFile() as errors:
        worker = subprocess.Popen([sys.executable, str(worker_path.resolve()), "--source-root", str(source_root.resolve()), *worker_arguments],
            cwd=source_root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, text=False, bufsize=0)
        selector = selectors.DefaultSelector()
        assert worker.stdin is not None and worker.stdout is not None
        os.set_blocking(worker.stdin.fileno(), False)
        os.set_blocking(worker.stdout.fileno(), False)
        stopped = None
        try:
            for request in requests:
                started = time.monotonic()
                raw = None
                if stopped is None:
                    try:
                        line = (canonical_json(request) + "\n").encode("utf-8")
                        if len(line) > 1024 * 1024:
                            raise IndustrialEvaluationError("worker request exceeds bounds")
                        selector.register(worker.stdin, selectors.EVENT_WRITE)
                        offset = 0
                        while offset < len(line):
                            remaining = deadline_seconds - (time.monotonic() - started)
                            if remaining <= 0 or not selector.select(remaining):
                                raise TimeoutError
                            try:
                                written = os.write(worker.stdin.fileno(), line[offset:offset + 64 * 1024])
                            except BlockingIOError:
                                continue
                            if written <= 0:
                                raise IndustrialEvaluationError("worker stopped accepting request bytes")
                            offset += written
                        selector.unregister(worker.stdin)
                        selector.register(worker.stdout, selectors.EVENT_READ)
                        output = bytearray()
                        while not output.endswith(b"\n"):
                            remaining = deadline_seconds - (time.monotonic() - started)
                            if remaining <= 0 or not selector.select(remaining):
                                raise TimeoutError
                            try:
                                data = os.read(worker.stdout.fileno(), 64 * 1024)
                            except BlockingIOError:
                                continue
                            if not data:
                                raise IndustrialEvaluationError("worker ended before a response")
                            output.extend(data)
                            if len(output) > 2 * 1024 * 1024:
                                raise IndustrialEvaluationError("worker response exceeds bounds")
                        selector.unregister(worker.stdout)
                        raw = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object)
                        if "worker_error" in raw or raw.get("case_id") != request["case_id"] or raw.get("variant") != request["variant"]:
                            raise IndustrialEvaluationError("worker protocol response differs")
                    except (OSError, ValueError, TimeoutError) as error:
                        stopped = "WorkerDeadlineExceeded" if isinstance(error, TimeoutError) else "WorkerProtocolFailed"
                        worker.kill()
                if raw is None or stopped is not None:
                    raw = {key: request[key] for key in ("case_id", "variant", "query_checksum", "query_vector_checksum")}
                    raw.update(chunks=[], trace=None, error=stopped, implementation_checksum=None,
                               duration_ms=round((time.monotonic() - started) * 1000, 3))
                captures.append(raw)
                if progress is not None:
                    progress({"variant": request["variant"], "case_id": request["case_id"],
                              "chunks": len(raw["chunks"]), "error": raw["error"]})
        finally:
            selector.close()
            worker.stdin.close()
            if worker.poll() is None:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
            worker.stdout.close()
    return captures
