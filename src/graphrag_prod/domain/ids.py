"""Stable identifiers derived from immutable identity inputs."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID, uuid5


# Project-specific namespace. Its value is permanent once IDs are published.
ID_NAMESPACE = UUID("f7177cb0-24f8-5e79-a02c-72f731ad46f3")
ID_SCHEME_VERSION = "1"


def _required(value: str, name: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _stable_id(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [ID_SCHEME_VERSION, kind, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return str(uuid5(ID_NAMESPACE, payload))


def _checksum(value: str) -> str:
    normalized = _required(value, "checksum").lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("checksum must be a hexadecimal SHA-256 digest")
    return normalized


def canonicalize_uri(uri: str) -> str:
    """Canonicalize a source URI without resolving network resources."""
    value = _required(uri, "uri")
    parsed = urlsplit(value)
    if not parsed.scheme:
        raise ValueError("uri must include a scheme")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname and scheme not in {"file", "urn"}:
        raise ValueError("uri must include a host")

    netloc = hostname
    if parsed.port is not None:
        default_port = (scheme == "http" and parsed.port == 80) or (
            scheme == "https" and parsed.port == 443
        )
        if not default_port:
            netloc = f"{netloc}:{parsed.port}"
    if parsed.username or parsed.password:
        raise ValueError("uri must not contain credentials")

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    canonical = SplitResult(scheme, netloc, path, parsed.query, "")
    return urlunsplit(canonical)


def content_checksum(content: str | bytes) -> str:
    """Return an exact SHA-256 checksum for the supplied source bytes."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def document_id(tenant_id: str, canonical_uri: str) -> str:
    return _stable_id(
        "document",
        _required(tenant_id, "tenant_id"),
        canonicalize_uri(canonical_uri),
    )


def version_id(
    document_identifier: str,
    normalized_checksum: str,
    original_checksum: str | None = None,
) -> str:
    return _stable_id(
        "document-version",
        _required(document_identifier, "document_id"),
        _checksum(normalized_checksum),
        _checksum(original_checksum or normalized_checksum),
    )


def chunk_id(
    version_identifier: str,
    splitter_signature: str,
    ordinal: int,
    char_start: int,
    char_end: int,
    checksum: str,
) -> str:
    if ordinal < 0 or char_start < 0 or char_end <= char_start:
        raise ValueError("chunk ordinal and character range are invalid")
    return _stable_id(
        "chunk",
        _required(version_identifier, "version_id"),
        _required(splitter_signature, "splitter_signature"),
        ordinal,
        char_start,
        char_end,
        _checksum(checksum),
    )


def mention_id(
    chunk_identifier: str,
    entity_type: str,
    char_start: int,
    char_end: int,
    surface: str,
    extractor_signature: str,
) -> str:
    if char_start < 0 or char_end <= char_start:
        raise ValueError("mention character range is invalid")
    return _stable_id(
        "mention",
        _required(chunk_identifier, "chunk_id"),
        _required(entity_type, "entity_type"),
        char_start,
        char_end,
        _required(surface, "surface"),
        _required(extractor_signature, "extractor_signature"),
    )


def entity_id(tenant_id: str, entity_type: str, canonical_key: str) -> str:
    """Create an ID from an adjudicated key, never from display name alone."""
    return _stable_id(
        "entity",
        _required(tenant_id, "tenant_id"),
        _required(entity_type, "entity_type"),
        _required(canonical_key, "canonical_key"),
    )


def embedding_space_id(
    provider: str,
    model: str,
    revision: str,
    dimensions: int,
    normalization: str,
) -> str:
    if dimensions <= 0:
        raise ValueError("embedding dimensions must be positive")
    return _stable_id(
        "embedding-space",
        _required(provider, "provider"),
        _required(model, "model"),
        _required(revision, "revision"),
        dimensions,
        _required(normalization, "normalization"),
    )


def chunk_embedding_id(chunk_identifier: str, space_identifier: str) -> str:
    return _stable_id(
        "chunk-embedding",
        _required(chunk_identifier, "chunk_id"),
        _required(space_identifier, "embedding_space_id"),
    )


def assertion_id(
    tenant_id: str,
    subject_entity_id: str,
    predicate: str,
    object_kind: str,
    object_reference: str,
    evidence_chunk_id: str,
    evidence_char_start: int,
    evidence_char_end: int,
    extractor_version: str,
    schema_version: str,
) -> str:
    if evidence_char_start < 0 or evidence_char_end <= evidence_char_start:
        raise ValueError("assertion evidence range is invalid")
    return _stable_id(
        "assertion",
        _required(tenant_id, "tenant_id"),
        _required(subject_entity_id, "subject_entity_id"),
        _required(predicate, "predicate"),
        _required(object_kind, "object_kind"),
        _required(object_reference, "object_reference"),
        _required(evidence_chunk_id, "evidence_chunk_id"),
        evidence_char_start,
        evidence_char_end,
        _required(extractor_version, "extractor_version"),
        _required(schema_version, "schema_version"),
    )


def pipeline_profile_id(
    normalizer_signature: str,
    splitter_signature: str,
    extractor_signature: str,
    prompt_signature: str,
    schema_signature: str,
    code_signature: str,
) -> str:
    """Identify a graph-building profile independently of any source version."""
    return _stable_id(
        "graph-pipeline-profile",
        _required(normalizer_signature, "normalizer_signature"),
        _required(splitter_signature, "splitter_signature"),
        _required(extractor_signature, "extractor_signature"),
        _required(prompt_signature, "prompt_signature"),
        _required(schema_signature, "schema_signature"),
        _required(code_signature, "code_signature"),
    )


def knowledge_snapshot_id(version_identifier: str, profile_identifier: str) -> str:
    return _stable_id(
        "knowledge-snapshot",
        _required(version_identifier, "version_id"),
        _required(profile_identifier, "profile_id"),
    )


def ingestion_job_id(tenant_id: str, operation: str, idempotency_key: str) -> str:
    return _stable_id(
        "ingestion-job",
        _required(tenant_id, "tenant_id"),
        _required(operation, "operation"),
        _required(idempotency_key, "idempotency_key"),
    )


def ingestion_task_id(job_identifier: str, chunk_identifier: str) -> str:
    return _stable_id(
        "ingestion-task",
        _required(job_identifier, "job_id"),
        _required(chunk_identifier, "chunk_id"),
    )


def derivation_artifact_id(
    tenant_id: str,
    kind: str,
    input_hash: str,
    profile_identifier: str,
) -> str:
    return _stable_id(
        "derivation-artifact",
        _required(tenant_id, "tenant_id"),
        _required(kind, "kind"),
        _checksum(input_hash),
        _required(profile_identifier, "profile_id"),
    )


def embedding_index_generation_id(
    tenant_id: str,
    embedding_space_identifier: str,
    generation_version: int,
) -> str:
    if isinstance(generation_version, bool) or not isinstance(generation_version, int):
        raise ValueError("generation_version must be a positive integer")
    if generation_version <= 0:
        raise ValueError("generation_version must be a positive integer")
    return _stable_id(
        "embedding-index-generation",
        _required(tenant_id, "tenant_id"),
        _required(embedding_space_identifier, "embedding_space_id"),
        generation_version,
    )
