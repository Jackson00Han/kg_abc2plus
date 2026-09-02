#!/usr/bin/env python3
"""Build or verify the compact, deterministic ``load-v1`` corpus.

Only a manifest and notice are committed.  The 24,000 versioned Chunk records
are generated as canonical JSONL streams when a load environment requests
materialization, keeping the repository small without weakening identity,
provenance, version, tenant, or access-control invariants.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterator, Mapping

from graphrag_prod.domain import (
    chunk_embedding_id,
    chunk_id,
    content_checksum,
    document_id,
    embedding_index_generation_id,
    embedding_space_id,
    entity_id,
    knowledge_snapshot_id,
    mention_id,
    pipeline_profile_id,
    version_id,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_DIR = ROOT / "datasets" / "load-v1"

DATASET_ID = "load-v1"
DATASET_VERSION = "1.0.2"
MANIFEST_SCHEMA_VERSION = "load-corpus-manifest-v1"
BUILDER_VERSION = "1.0.2"
TENANT_COUNT = 5
GROUPS_PER_TENANT = 4
PRIMARY_TENANT_NUMBER = 1
PRIMARY_TENANT_DOCUMENTS = 200
CANARY_DOCUMENTS_PER_TENANT = 10
VERSIONS_PER_DOCUMENT = 2
CHUNKS_PER_VERSION = 50
ACTIVE_VERSION_NUMBER = 2
SPLITTER_SIGNATURE = "load-record-splitter:v1"
NORMALIZER_SIGNATURE = "utf8-nfc-lf:v1"
EXTRACTOR_SIGNATURE = "synthetic-load-document-entity-extractor:v1"
PROMPT_SIGNATURE = "synthetic-load-noop-prompt:v1"
SCHEMA_SIGNATURE = "company-filings:v1"
CODE_SIGNATURE = "scripts/build_load_corpus.py:v1.0.0"
TEXT_TEMPLATE_VERSION = "synthetic-load-evidence:v1"
VECTOR_DERIVATION = "sha256-two-coordinate-l2:v1"
EMBEDDING_PROVIDER = "fixture"
EMBEDDING_MODEL = "deterministic-load-sparse"
EMBEDDING_REVISION = "load-v1.0"
EMBEDDING_DIMENSIONS = 64
EMBEDDING_NORMALIZATION = "l2"
EMBEDDING_SPACE_ID = embedding_space_id(
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    EMBEDDING_REVISION,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_NORMALIZATION,
)
PIPELINE_PROFILE_ID = pipeline_profile_id(
    NORMALIZER_SIGNATURE,
    SPLITTER_SIGNATURE,
    EXTRACTOR_SIGNATURE,
    PROMPT_SIGNATURE,
    SCHEMA_SIGNATURE,
    CODE_SIGNATURE,
)
PRIMARY_TENANT_ID = f"load-tenant-{PRIMARY_TENANT_NUMBER:02d}"
RETRIEVAL_QUERY_COUNT = 64
RETRIEVAL_MINIMUM_ANCHOR_COSINE = 0.75
RETRIEVAL_ANCHOR_SELECTION = (
    "public-one-per-document-unique-cosine-neighborhood-v1"
)
SCENARIOS = (
    "steady_state",
    "version_update",
    "delete_recreate",
    "interrupted_recovery",
)
MATERIALIZED_FILES = frozenset(
    {
        "NOTICE.txt",
        "chunks.jsonl",
        "documents.jsonl",
        "entities.jsonl",
        "manifest.json",
        "mentions.jsonl",
    }
)
COMPACT_FILES = frozenset({"NOTICE.txt", "manifest.json"})

NOTICE = (
    "SYNTHETIC LOAD DATA ONLY\n"
    "\n"
    "load-v1 is a deterministic generated workload, not a real filing, customer\n"
    "record, or provider-quality dataset. The repository stores only this notice\n"
    "and a checksummed generation manifest. Materialized JSONL belongs in an\n"
    "explicit external output directory and must not be committed.\n"
).encode("utf-8")


@dataclass(frozen=True, slots=True)
class VersionBundle:
    """One immutable Document Version plus its contiguous Chunks."""

    document: dict[str, Any]
    chunks: tuple[dict[str, Any], ...]
    entity: dict[str, Any]
    mentions: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class GraphRecordBundle:
    """One-pass records for direct Document-to-Embedding bulk loading."""

    document: dict[str, Any]
    version: dict[str, Any]
    snapshot: dict[str, Any]
    chunks: tuple[dict[str, Any], ...]
    embeddings: tuple[dict[str, Any], ...]
    entities: tuple[dict[str, Any], ...]
    mentions: tuple[dict[str, Any], ...]
    assertions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class StreamSummary:
    records: int
    size_bytes: int
    sha256: str

    def as_dict(self, path: str) -> dict[str, Any]:
        return {
            "path": path,
            "records": self.records,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize one canonical JSONL record."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tenant_id(tenant_number: int) -> str:
    return f"load-tenant-{tenant_number:02d}"


def _documents_for_tenant(tenant_number: int) -> int:
    if tenant_number == PRIMARY_TENANT_NUMBER:
        return PRIMARY_TENANT_DOCUMENTS
    return CANARY_DOCUMENTS_PER_TENANT


def _global_document_index(
    tenant_number: int,
    document_number: int,
) -> int:
    if tenant_number == PRIMARY_TENANT_NUMBER:
        return document_number - 1
    return (
        PRIMARY_TENANT_DOCUMENTS
        + (tenant_number - 2) * CANARY_DOCUMENTS_PER_TENANT
        + document_number
        - 1
    )


def _access(
    tenant_id: str,
    document_number: int,
) -> tuple[str, tuple[str, ...]]:
    if document_number % 2 == 1:
        return "public", (f"{tenant_id}-public",)
    protected_index = document_number // 2 - 1
    primary_number = protected_index % GROUPS_PER_TENANT + 1
    groups = [f"{tenant_id}-group-{primary_number:02d}"]
    if document_number % 8 == 0:
        secondary_number = primary_number % GROUPS_PER_TENANT + 1
        groups.append(f"{tenant_id}-group-{secondary_number:02d}")
    return "protected", tuple(sorted(groups))


def _scenario(global_document_index: int) -> str:
    return SCENARIOS[global_document_index % len(SCENARIOS)]


def deterministic_vector(chunk_identifier: str) -> tuple[float, ...]:
    """Return a stable, exactly unit-length sparse fixture vector."""

    seed = hashlib.sha256(
        f"{VECTOR_DERIVATION}:{chunk_identifier}".encode("utf-8")
    ).digest()
    first_index = int.from_bytes(seed[0:4], "big") % EMBEDDING_DIMENSIONS
    second_index = int.from_bytes(seed[4:8], "big") % (
        EMBEDDING_DIMENSIONS - 1
    )
    if second_index >= first_index:
        second_index += 1
    first_sign = -1.0 if seed[8] & 1 else 1.0
    second_sign = -1.0 if seed[9] & 1 else 1.0
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[first_index] = first_sign * 0.8
    values[second_index] = second_sign * 0.6
    vector = tuple(values)
    if not math.isclose(math.hypot(*vector), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise AssertionError("deterministic load vector must have unit norm")
    return vector


def _vector_checksum(vector: tuple[float, ...]) -> str:
    payload = json.dumps(
        vector,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return content_checksum(payload)


def _vector_neighborhood_key(chunk_identifier: str) -> tuple[tuple[int, int], ...]:
    """Identify every sparse vector within the 0.75 production recall gate.

    The versioned ``sha256-two-coordinate-l2:v1`` derivation has exactly two
    non-zero coordinates. Two such vectors can reach cosine 0.75 only when
    both coordinate/sign pairs match; swapping the 0.8 and 0.6 weights still
    scores 0.96 and therefore belongs to the same neighborhood.
    """

    key = tuple(
        (index, 1 if value > 0 else -1)
        for index, value in enumerate(deterministic_vector(chunk_identifier))
        if value
    )
    if len(key) != 2:
        raise AssertionError("deterministic load vector must have two supports")
    return key


def _chunk_text(
    *,
    tenant_id: str,
    document_key: str,
    version_number: int,
    ordinal: int,
    scenario: str,
    access_mode: str,
    groups: tuple[str, ...],
    global_document_index: int,
) -> str:
    value = (
        (global_document_index + 1) * 100_000
        + version_number * 1_000
        + ordinal
    )
    return (
        "Load-v1 synthetic evidence; "
        f"tenant={tenant_id}; document={document_key}; "
        f"version={version_number}; chunk={ordinal:03d}; "
        f"scenario={scenario}; access_mode={access_mode}; "
        f"access={','.join(groups)}; "
        f"measure={value} units; fiscal_year={2022 + version_number}.\n"
    )


def _version_bundle(
    tenant_number: int,
    document_number: int,
    version_number: int,
) -> VersionBundle:
    tenant_id = _tenant_id(tenant_number)
    document_key = f"{tenant_id}:document-{document_number:03d}"
    global_document_index = _global_document_index(
        tenant_number,
        document_number,
    )
    scenario = _scenario(global_document_index)
    access_mode, groups = _access(tenant_id, document_number)
    canonical_uri = (
        "urn:sample-graphrag:synthetic:load-v1:"
        f"{tenant_id}:document-{document_number:03d}"
    )
    document_identifier = document_id(tenant_id, canonical_uri)
    texts = tuple(
        _chunk_text(
            tenant_id=tenant_id,
            document_key=document_key,
            version_number=version_number,
            ordinal=ordinal,
            scenario=scenario,
            access_mode=access_mode,
            groups=groups,
            global_document_index=global_document_index,
        )
        for ordinal in range(CHUNKS_PER_VERSION)
    )
    normalized_text = "".join(texts)
    checksum = content_checksum(normalized_text)
    version_identifier = version_id(document_identifier, checksum, checksum)
    year = 2022 + version_number
    created_at = "2023-01-01T00:00:00+00:00"
    published_at = f"{year}-11-15T00:00:00+00:00"
    ingested_at = f"{year}-11-16T00:00:00+00:00"
    active = version_number == ACTIVE_VERSION_NUMBER
    document = {
        "access_mode": access_mode,
        "access_groups": list(groups),
        "access_policy_id": f"{tenant_id}:load-policy",
        "access_policy_version": 1,
        "active": active,
        "canonical_uri": canonical_uri,
        "created_at": created_at,
        "document_id": document_identifier,
        "document_key": document_key,
        "ingested_at": ingested_at,
        "language": "en",
        "lifecycle_scenario": scenario,
        "mime_type": "text/plain",
        "normalized_text": normalized_text,
        "operation_key": f"load-v1:{document_key}:v{version_number}",
        "original_checksum": checksum,
        "published_at": published_at,
        "source_name": "sample-graphrag deterministic synthetic load corpus",
        "tenant_id": tenant_id,
        "title": (
            f"Synthetic {tenant_id} load document {document_number:03d}"
        ),
        "version_checksum": checksum,
        "version_id": version_identifier,
        "version_number": version_number,
    }

    chunks: list[dict[str, Any]] = []
    cursor = 0
    for ordinal, text in enumerate(texts):
        char_start = cursor
        char_end = char_start + len(text)
        checksum_value = content_checksum(text)
        chunk_identifier = chunk_id(
            version_identifier,
            SPLITTER_SIGNATURE,
            ordinal,
            char_start,
            char_end,
            checksum_value,
        )
        vector = deterministic_vector(chunk_identifier)
        chunks.append(
            {
                "access_mode": access_mode,
                "access_groups": list(groups),
                "access_policy_id": f"{tenant_id}:load-policy",
                "access_policy_version": 1,
                "active": active,
                "char_end": char_end,
                "char_start": char_start,
                "checksum": checksum_value,
                "chunk_id": chunk_identifier,
                "chunk_key": (
                    f"{document_key}:v{version_number}:chunk-{ordinal:03d}"
                ),
                "document_id": document_identifier,
                "embedding_dimensions": EMBEDDING_DIMENSIONS,
                "embedding_id": chunk_embedding_id(
                    chunk_identifier, EMBEDDING_SPACE_ID
                ),
                "embedding_model": EMBEDDING_MODEL,
                "embedding_normalization": EMBEDDING_NORMALIZATION,
                "embedding_provider": EMBEDDING_PROVIDER,
                "embedding_revision": EMBEDDING_REVISION,
                "embedding_space_id": EMBEDDING_SPACE_ID,
                "lifecycle_scenario": scenario,
                "ordinal": ordinal,
                "page_number": ordinal // 10 + 1,
                "section": f"Load section {ordinal // 10 + 1}",
                "splitter_version": SPLITTER_SIGNATURE,
                "tenant_id": tenant_id,
                "text": text,
                "vector": list(vector),
                "vector_checksum": _vector_checksum(vector),
                "version_id": version_identifier,
                "version_number": version_number,
            }
        )
        cursor = char_end
    if cursor != len(normalized_text):
        raise AssertionError("load Chunk ranges must cover the complete source")
    canonical_key = f"name:{document_key}"
    entity_identifier = entity_id(tenant_id, "Company", canonical_key)
    entity = {
        "aliases": [],
        "canonical_key": canonical_key,
        "canonical_name": document_key,
        "document_id": document_identifier,
        "entity_id": entity_identifier,
        "entity_type": "Company",
        "tenant_id": tenant_id,
    }
    surface = f"document={document_key}"
    snapshot_identifier = knowledge_snapshot_id(
        version_identifier,
        PIPELINE_PROFILE_ID,
    )
    mentions: list[dict[str, Any]] = []
    for chunk in chunks:
        relative_start = chunk["text"].index(surface)
        absolute_start = chunk["char_start"] + relative_start
        absolute_end = absolute_start + len(surface)
        mentions.append(
            {
                "active": active,
                "char_end": absolute_end,
                "char_start": absolute_start,
                "chunk_id": chunk["chunk_id"],
                "confidence": 1.0,
                "document_id": document_identifier,
                "entity_id": entity_identifier,
                "entity_type": "Company",
                "extractor_version": EXTRACTOR_SIGNATURE,
                "mention_id": mention_id(
                    chunk["chunk_id"],
                    "Company",
                    absolute_start,
                    absolute_end,
                    surface,
                    EXTRACTOR_SIGNATURE,
                ),
                "relative_char_end": relative_start + len(surface),
                "relative_char_start": relative_start,
                "snapshot_id": snapshot_identifier,
                "surface": surface,
                "tenant_id": tenant_id,
                "version_id": version_identifier,
            }
        )
    return VersionBundle(
        document=document,
        chunks=tuple(chunks),
        entity=entity,
        mentions=tuple(mentions),
    )


def _tenant_numbers(tenant_id: str | None) -> tuple[int, ...]:
    if tenant_id is None:
        return tuple(range(1, TENANT_COUNT + 1))
    matches = tuple(
        number
        for number in range(1, TENANT_COUNT + 1)
        if _tenant_id(number) == tenant_id
    )
    if not matches:
        raise ValueError(f"unknown load-v1 tenant_id: {tenant_id}")
    return matches


def iter_version_bundles(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[VersionBundle]:
    """Yield stable records in tenant, Document, Version order.

    This is the lowest-level one-pass API. Callers can select a tenant and the
    active version without materializing either committed or temporary JSONL.
    """

    for tenant_number in _tenant_numbers(tenant_id):
        for document_number in range(
            1,
            _documents_for_tenant(tenant_number) + 1,
        ):
            for version_number in range(1, VERSIONS_PER_DOCUMENT + 1):
                if active_only and version_number != ACTIVE_VERSION_NUMBER:
                    continue
                yield _version_bundle(
                    tenant_number,
                    document_number,
                    version_number,
                )


def graph_records_from_bundle(bundle: VersionBundle) -> GraphRecordBundle:
    """Project a generated Version into graph-node records without I/O."""

    source = bundle.document
    document = {
        key: source[key]
        for key in (
            "access_groups",
            "access_mode",
            "access_policy_id",
            "access_policy_version",
            "canonical_uri",
            "created_at",
            "document_id",
            "document_key",
            "lifecycle_scenario",
            "source_name",
            "tenant_id",
            "title",
        )
    }
    version = {
        "active": source["active"],
        "checksum": source["version_checksum"],
        "document_id": source["document_id"],
        "ingested_at": source["ingested_at"],
        "language": source["language"],
        "lifecycle_scenario": source["lifecycle_scenario"],
        "mime_type": source["mime_type"],
        "normalized_text": source["normalized_text"],
        "operation_key": source["operation_key"],
        "original_checksum": source["original_checksum"],
        "published_at": source["published_at"],
        "tenant_id": source["tenant_id"],
        "version_id": source["version_id"],
        "version_number": source["version_number"],
    }
    snapshot_manifest = tuple(
        sorted(
            (
                {
                    "assertions": [],
                    "chunk_id": chunk["chunk_id"],
                    "entities": [
                        {
                            "aliases": bundle.entity["aliases"],
                            "canonical_name": bundle.entity["canonical_name"],
                            "entity_id": bundle.entity["entity_id"],
                        }
                    ],
                    "mentions": [
                        {
                            "confidence": mention["confidence"],
                            "entity_id": mention["entity_id"],
                            "mention_id": mention["mention_id"],
                        }
                    ],
                    "page_number": chunk["page_number"],
                    "section": chunk["section"],
                }
                for chunk, mention in zip(
                    bundle.chunks,
                    bundle.mentions,
                    strict=True,
                )
            ),
            key=lambda item: item["chunk_id"],
        )
    )
    snapshot_manifest_hash = content_checksum(
        json.dumps(
            snapshot_manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    snapshot_identifier = knowledge_snapshot_id(
        source["version_id"],
        PIPELINE_PROFILE_ID,
    )
    snapshot = {
        "active": source["active"],
        "created_at": source["ingested_at"],
        "document_id": source["document_id"],
        "expected_chunk_count": len(bundle.chunks),
        "manifest_hash": snapshot_manifest_hash,
        "profile_id": PIPELINE_PROFILE_ID,
        "snapshot_id": snapshot_identifier,
        "tenant_id": source["tenant_id"],
        "version_id": source["version_id"],
    }
    chunks: list[dict[str, Any]] = []
    embeddings: list[dict[str, Any]] = []
    for item in bundle.chunks:
        chunks.append(
            {
                key: item[key]
                for key in (
                    "access_groups",
                    "access_mode",
                    "access_policy_id",
                    "access_policy_version",
                    "active",
                    "char_end",
                    "char_start",
                    "checksum",
                    "chunk_id",
                    "chunk_key",
                    "document_id",
                    "lifecycle_scenario",
                    "ordinal",
                    "page_number",
                    "section",
                    "splitter_version",
                    "tenant_id",
                    "text",
                    "version_id",
                    "version_number",
                )
            }
        )
        chunks[-1]["snapshot_id"] = snapshot_identifier
        embeddings.append(
            {
                "active": item["active"],
                "chunk_id": item["chunk_id"],
                "created_at": source["ingested_at"],
                "dimensions": item["embedding_dimensions"],
                "embedding_id": item["embedding_id"],
                "embedding_space_id": item["embedding_space_id"],
                "model": item["embedding_model"],
                "normalization": item["embedding_normalization"],
                "provider": item["embedding_provider"],
                "revision": item["embedding_revision"],
                "snapshot_id": snapshot_identifier,
                "tenant_id": item["tenant_id"],
                "vector": item["vector"],
                "vector_checksum": item["vector_checksum"],
            }
        )
    return GraphRecordBundle(
        document=document,
        version=version,
        snapshot=snapshot,
        chunks=tuple(chunks),
        embeddings=tuple(embeddings),
        entities=(dict(bundle.entity),),
        mentions=tuple(dict(item) for item in bundle.mentions),
    )


def iter_graph_record_bundles(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[GraphRecordBundle]:
    """Yield Document/Version/Snapshot/Chunk/Embedding records in one pass."""

    for bundle in iter_version_bundles(
        tenant_id=tenant_id,
        active_only=active_only,
    ):
        yield graph_records_from_bundle(bundle)


def iter_documents(
    *,
    tenant_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield each logical Document once, ordered by tenant and URI."""

    for bundle in iter_graph_record_bundles(
        tenant_id=tenant_id,
        active_only=True,
    ):
        yield bundle.document


def iter_versions(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[dict[str, Any]]:
    for bundle in iter_graph_record_bundles(
        tenant_id=tenant_id,
        active_only=active_only,
    ):
        yield bundle.version


def iter_snapshots(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[dict[str, Any]]:
    for bundle in iter_graph_record_bundles(
        tenant_id=tenant_id,
        active_only=active_only,
    ):
        yield bundle.snapshot


def iter_chunks(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[dict[str, Any]]:
    for bundle in iter_graph_record_bundles(
        tenant_id=tenant_id,
        active_only=active_only,
    ):
        yield from bundle.chunks


def iter_embeddings(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[dict[str, Any]]:
    for bundle in iter_graph_record_bundles(
        tenant_id=tenant_id,
        active_only=active_only,
    ):
        yield from bundle.embeddings


def iter_entities(
    *,
    tenant_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield each stable per-Document Company entity once."""

    for bundle in iter_version_bundles(
        tenant_id=tenant_id,
        active_only=True,
    ):
        yield bundle.entity


def iter_mentions(
    *,
    tenant_id: str | None = None,
    active_only: bool = False,
) -> Iterator[dict[str, Any]]:
    for bundle in iter_version_bundles(
        tenant_id=tenant_id,
        active_only=active_only,
    ):
        yield from bundle.mentions


def _summaries_and_samples() -> tuple[
    StreamSummary,
    StreamSummary,
    StreamSummary,
    StreamSummary,
    dict[str, dict[str, Any]],
]:
    document_hash = hashlib.sha256()
    chunk_hash = hashlib.sha256()
    entity_hash = hashlib.sha256()
    mention_hash = hashlib.sha256()
    document_records = 0
    chunk_records = 0
    entity_records = 0
    mention_records = 0
    document_bytes = 0
    chunk_bytes = 0
    entity_bytes = 0
    mention_bytes = 0
    first_document: dict[str, Any] | None = None
    last_document: dict[str, Any] | None = None
    first_chunk: dict[str, Any] | None = None
    last_chunk: dict[str, Any] | None = None
    first_entity: dict[str, Any] | None = None
    last_entity: dict[str, Any] | None = None
    first_mention: dict[str, Any] | None = None
    last_mention: dict[str, Any] | None = None

    for bundle in iter_version_bundles():
        document_payload = canonical_json_bytes(bundle.document)
        document_hash.update(document_payload)
        document_records += 1
        document_bytes += len(document_payload)
        document_sample = {
            key: bundle.document[key]
            for key in (
                "document_key",
                "document_id",
                "version_id",
                "version_number",
                "tenant_id",
                "active",
            )
        }
        first_document = first_document or document_sample
        last_document = document_sample
        if bundle.document["version_number"] == 1:
            entity_payload = canonical_json_bytes(bundle.entity)
            entity_hash.update(entity_payload)
            entity_records += 1
            entity_bytes += len(entity_payload)
            entity_sample = {
                key: bundle.entity[key]
                for key in (
                    "canonical_key",
                    "document_id",
                    "entity_id",
                    "tenant_id",
                )
            }
            first_entity = first_entity or entity_sample
            last_entity = entity_sample
        for chunk in bundle.chunks:
            chunk_payload = canonical_json_bytes(chunk)
            chunk_hash.update(chunk_payload)
            chunk_records += 1
            chunk_bytes += len(chunk_payload)
            chunk_sample = {
                key: chunk[key]
                for key in (
                    "chunk_key",
                    "chunk_id",
                    "version_id",
                    "tenant_id",
                    "active",
                    "checksum",
                    "vector_checksum",
                )
            }
            first_chunk = first_chunk or chunk_sample
            last_chunk = chunk_sample
        for mention in bundle.mentions:
            mention_payload = canonical_json_bytes(mention)
            mention_hash.update(mention_payload)
            mention_records += 1
            mention_bytes += len(mention_payload)
            mention_sample = {
                key: mention[key]
                for key in (
                    "chunk_id",
                    "entity_id",
                    "mention_id",
                    "surface",
                    "tenant_id",
                )
            }
            first_mention = first_mention or mention_sample
            last_mention = mention_sample

    if any(
        value is None
        for value in (
            first_document,
            last_document,
            first_chunk,
            last_chunk,
            first_entity,
            last_entity,
            first_mention,
            last_mention,
        )
    ):
        raise AssertionError("load corpus streams must not be empty")
    return (
        StreamSummary(
            records=document_records,
            size_bytes=document_bytes,
            sha256=document_hash.hexdigest(),
        ),
        StreamSummary(
            records=chunk_records,
            size_bytes=chunk_bytes,
            sha256=chunk_hash.hexdigest(),
        ),
        StreamSummary(
            records=entity_records,
            size_bytes=entity_bytes,
            sha256=entity_hash.hexdigest(),
        ),
        StreamSummary(
            records=mention_records,
            size_bytes=mention_bytes,
            sha256=mention_hash.hexdigest(),
        ),
        {
            "first_chunk": first_chunk,
            "first_document_version": first_document,
            "first_entity": first_entity,
            "first_mention": first_mention,
            "last_chunk": last_chunk,
            "last_document_version": last_document,
            "last_entity": last_entity,
            "last_mention": last_mention,
        },
    )


def _workload_canaries() -> dict[str, Any]:
    """Select stable active records for authorization and deletion checks."""

    principal_groups = (
        f"{PRIMARY_TENANT_ID}-public",
        f"{PRIMARY_TENANT_ID}-group-01",
    )
    principal_group_set = set(principal_groups)
    protected_same_tenant_chunk_ids: list[str] = []
    protected_documents: set[str] = set()
    cross_tenant_chunk_ids: list[str] = []
    sampled_cross_tenants: set[str] = set()
    deletion_candidate: dict[str, Any] | None = None
    primary_active_chunks = 0
    primary_visible_chunks = 0
    cross_tenant_active_chunks = 0

    for bundle in iter_version_bundles(active_only=True):
        document = bundle.document
        if document["tenant_id"] == PRIMARY_TENANT_ID:
            primary_active_chunks += len(bundle.chunks)
            if principal_group_set.intersection(document["access_groups"]):
                primary_visible_chunks += len(bundle.chunks)
        else:
            cross_tenant_active_chunks += len(bundle.chunks)
        if (
            document["tenant_id"] == PRIMARY_TENANT_ID
            and document["access_mode"] == "protected"
            and not principal_group_set.intersection(document["access_groups"])
            and document["document_id"] not in protected_documents
            and len(protected_same_tenant_chunk_ids) < 4
        ):
            protected_documents.add(document["document_id"])
            protected_same_tenant_chunk_ids.append(bundle.chunks[0]["chunk_id"])
        if (
            document["tenant_id"] != PRIMARY_TENANT_ID
            and document["tenant_id"] not in sampled_cross_tenants
        ):
            sampled_cross_tenants.add(document["tenant_id"])
            cross_tenant_chunk_ids.append(bundle.chunks[0]["chunk_id"])
        if (
            deletion_candidate is None
            and document["tenant_id"] == _tenant_id(TENANT_COUNT)
            and document["document_key"].endswith("document-010")
        ):
            deletion_candidate = {
                "active_chunk_ids": [
                    bundle.chunks[0]["chunk_id"],
                    bundle.chunks[-1]["chunk_id"],
                ],
                "active_snapshot_id": knowledge_snapshot_id(
                    document["version_id"],
                    PIPELINE_PROFILE_ID,
                ),
                "active_version_id": document["version_id"],
                "canonical_uri": document["canonical_uri"],
                "document_id": document["document_id"],
                "tenant_id": document["tenant_id"],
            }

    if (
        len(protected_same_tenant_chunk_ids) != 4
        or len(cross_tenant_chunk_ids) != TENANT_COUNT - 1
        or deletion_candidate is None
    ):
        raise AssertionError("load-v1 authorization canaries are incomplete")
    return {
        "cross_tenant_chunk_ids": cross_tenant_chunk_ids,
        "deletion_candidate": deletion_candidate,
        "load_principal_acl": {
            "access_groups": sorted(principal_groups),
            "cross_tenant_active_chunks": cross_tenant_active_chunks,
            "cross_tenant_active_embeddings": cross_tenant_active_chunks,
            "denied_same_tenant_active_chunks": (
                primary_active_chunks - primary_visible_chunks
            ),
            "denied_same_tenant_active_embeddings": (
                primary_active_chunks - primary_visible_chunks
            ),
            "tenant_id": PRIMARY_TENANT_ID,
            "total_same_tenant_active_chunks": primary_active_chunks,
            "total_same_tenant_active_embeddings": primary_active_chunks,
            "visible_same_tenant_active_chunks": primary_visible_chunks,
            "visible_same_tenant_active_embeddings": primary_visible_chunks,
        },
        "load_principal_groups": list(principal_groups),
        "primary_load_tenant": PRIMARY_TENANT_ID,
        "protected_same_tenant_chunk_ids": protected_same_tenant_chunk_ids,
    }


def build_retrieval_workload() -> dict[str, Any]:
    """Return the committed semantic load-query contract for Stage 9."""

    groups = [
        f"{PRIMARY_TENANT_ID}-public",
        f"{PRIMARY_TENANT_ID}-group-01",
    ]
    principal_groups = frozenset(groups)
    visible_chunks = [
        item
        for item in iter_chunks(tenant_id=PRIMARY_TENANT_ID, active_only=True)
        if principal_groups.intersection(item["access_groups"])
    ]
    neighborhood_counts: dict[tuple[tuple[int, int], ...], int] = {}
    for item in visible_chunks:
        key = _vector_neighborhood_key(item["chunk_id"])
        neighborhood_counts[key] = neighborhood_counts.get(key, 0) + 1
    public_anchor_by_document: dict[str, dict[str, Any]] = {}
    for item in visible_chunks:
        if (
            item["access_mode"] == "public"
            and neighborhood_counts[
                _vector_neighborhood_key(item["chunk_id"])
            ]
            == 1
        ):
            public_anchor_by_document.setdefault(item["document_id"], item)
    public_chunks = list(public_anchor_by_document.values())[:RETRIEVAL_QUERY_COUNT]
    if len(public_chunks) != RETRIEVAL_QUERY_COUNT:
        raise AssertionError(
            "load-v1 lacks enough public anchors with a unique recall neighborhood"
        )
    queries = []
    for index, item in enumerate(public_chunks):
        vector = deterministic_vector(item["chunk_id"])
        queries.append(
            {
                "case_id": f"load-anchor-{index:02d}",
                "embedding_space_id": EMBEDDING_SPACE_ID,
                "expected_chunk_ids": [item["chunk_id"]],
                "expected_version_id": item["version_id"],
                "query_text": item["text"].rstrip("\n"),
                "query_vector_checksum": _vector_checksum(vector),
                "tenant_id": PRIMARY_TENANT_ID,
            }
        )
    return {
        "anchor_selection": RETRIEVAL_ANCHOR_SELECTION,
        "dataset_id": DATASET_ID,
        "minimum_anchor_cosine": RETRIEVAL_MINIMUM_ANCHOR_COSINE,
        "principal": {
            "groups": groups,
            "tenant_id": PRIMARY_TENANT_ID,
        },
        "queries": queries,
        "query_count": len(queries),
        "schema_version": "load-retrieval-workload-v1",
        "vector_derivation": VECTOR_DERIVATION,
    }


def _graph_expectations(
    tenant_document_counts: Mapping[str, int],
) -> dict[str, Any]:
    """Describe exact graph shapes on both sides of generation activation."""

    document_count = sum(tenant_document_counts.values())
    version_count = document_count * VERSIONS_PER_DOCUMENT
    total_chunks = version_count * CHUNKS_PER_VERSION
    active_chunks = document_count * CHUNKS_PER_VERSION
    tenant_count = len(tenant_document_counts)
    before_generation_label_counts = {
        "Chunk": total_chunks,
        "ChunkEmbedding": total_chunks,
        "Document": document_count,
        "DocumentVersion": version_count,
        "Entity": document_count,
        "EntityMention": total_chunks,
        "GraphGovernancePolicy": 1,
        "GraphPipelineProfile": 1,
        "IngestionJob": version_count,
        "InitialLoadJob": version_count,
        "KnowledgeSnapshot": version_count,
        "TenantCorpusState": tenant_count,
    }
    after_generation_label_counts = {
        **before_generation_label_counts,
        "EmbeddingIndexGeneration": tenant_count,
    }
    for tenant_id, tenant_documents in sorted(tenant_document_counts.items()):
        generation_id = embedding_index_generation_id(
            tenant_id,
            EMBEDDING_SPACE_ID,
            1,
        )
        generation_label = (
            "EmbeddingGeneration_" + generation_id.replace("-", "")[:24]
        )
        after_generation_label_counts[generation_label] = (
            tenant_documents * CHUNKS_PER_VERSION
        )

    # Per Version: five core provenance edges, one HAS_CHUNK,
    # INCLUDES_CHUNK and HAS_EMBEDDING edge per Chunk, one snapshot Entity
    # membership, and three provenance edges per EntityMention. Each Document
    # has two active pointers; each tenant has one active generation pointer.
    before_generation_relationship_count = (
        5 * version_count
        + 3 * total_chunks
        + version_count
        + 3 * total_chunks
        + 2 * document_count
    )
    before_generation_node_count = (
        tenant_count
        + version_count
        + 1
        + 1
        + document_count
        + version_count
        + version_count
        + total_chunks
        + total_chunks
        + document_count
        + total_chunks
    )
    after_generation_relationship_count = (
        before_generation_relationship_count + tenant_count
    )
    after_generation_node_count = before_generation_node_count + tenant_count
    if active_chunks != sum(
        count
        for label, count in after_generation_label_counts.items()
        if label.startswith("EmbeddingGeneration_")
    ):
        raise AssertionError("active embedding label coverage is inconsistent")
    return {
        "before_generation_activation": {
            "business_node_count": before_generation_node_count,
            "business_relationship_count": before_generation_relationship_count,
            "label_counts": dict(sorted(before_generation_label_counts.items())),
        },
        "after_generation_activation": {
            "business_node_count": after_generation_node_count,
            "business_relationship_count": after_generation_relationship_count,
            "label_counts": dict(sorted(after_generation_label_counts.items())),
        }
    }


def build_manifest() -> dict[str, Any]:
    """Stream the logical corpus and return its compact immutable manifest."""

    (
        document_summary,
        chunk_summary,
        entity_summary,
        mention_summary,
        samples,
    ) = _summaries_and_samples()
    tenant_document_counts = {
        _tenant_id(number): _documents_for_tenant(number)
        for number in range(1, TENANT_COUNT + 1)
    }
    document_count = sum(tenant_document_counts.values())
    version_count = document_count * VERSIONS_PER_DOCUMENT
    total_chunks = version_count * CHUNKS_PER_VERSION
    active_chunks = document_count * CHUNKS_PER_VERSION
    parameters = {
        "active_version_number": ACTIVE_VERSION_NUMBER,
        "canary_documents_per_tenant": CANARY_DOCUMENTS_PER_TENANT,
        "chunks_per_version": CHUNKS_PER_VERSION,
        "groups_per_tenant": GROUPS_PER_TENANT,
        "primary_tenant_documents": PRIMARY_TENANT_DOCUMENTS,
        "primary_tenant_id": PRIMARY_TENANT_ID,
        "tenant_count": TENANT_COUNT,
        "versions_per_document": VERSIONS_PER_DOCUMENT,
    }
    streams = {
        "chunks": chunk_summary.as_dict("chunks.jsonl"),
        "documents": document_summary.as_dict("documents.jsonl"),
        "entities": entity_summary.as_dict("entities.jsonl"),
        "mentions": mention_summary.as_dict("mentions.jsonl"),
    }
    content_identity = {
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "parameters": parameters,
        "streams": streams,
    }
    tenants = [_tenant_id(number) for number in range(1, TENANT_COUNT + 1)]
    access_groups = [
        group
        for tenant_id in tenants
        for group in (
            f"{tenant_id}-public",
            *(
                f"{tenant_id}-group-{group_number:02d}"
                for group_number in range(1, GROUPS_PER_TENANT + 1)
            ),
        )
    ]
    scenario_documents = {
        scenario: sum(
            _scenario(index) == scenario for index in range(document_count)
        )
        for scenario in SCENARIOS
    }
    public_document_count = sum(
        sum(
            document_number % 2 == 1
            for document_number in range(1, count + 1)
        )
        for count in tenant_document_counts.values()
    )
    protected_document_count = document_count - public_document_count
    primary_public_documents = sum(
        document_number % 2 == 1
        for document_number in range(1, PRIMARY_TENANT_DOCUMENTS + 1)
    )
    primary_protected_documents = (
        PRIMARY_TENANT_DOCUMENTS - primary_public_documents
    )
    workload_canaries = _workload_canaries()
    return {
        "content_sha256": _sha256(canonical_json_bytes(content_identity)),
        "counts": {
            "access_groups": len(access_groups),
            "active_chunks": active_chunks,
            "assertions": 0,
            "documents": document_count,
            "entities": document_count,
            "historical_chunks": total_chunks - active_chunks,
            "load_items": active_chunks,
            "mentions": total_chunks,
            "tenants": TENANT_COUNT,
            "total_chunks": total_chunks,
            "versions": version_count,
        },
        "coverage": {
            "access_groups": access_groups,
            "access_modes": {
                "protected_active_chunks": (
                    protected_document_count * CHUNKS_PER_VERSION
                ),
                "public_active_chunks": public_document_count * CHUNKS_PER_VERSION,
            },
            "active_version_number": ACTIVE_VERSION_NUMBER,
            "canary_tenants": tenants[1:],
            "cross_tenant_chunk_ids": workload_canaries[
                "cross_tenant_chunk_ids"
            ],
            "deletion_candidate": workload_canaries["deletion_candidate"],
            "documents_by_tenant": tenant_document_counts,
            "lifecycle_scenarios": scenario_documents,
            "multi_group_documents": sum(
                count // 8 for count in tenant_document_counts.values()
            ),
            "load_principal_groups": workload_canaries[
                "load_principal_groups"
            ],
            "load_principal_acl": workload_canaries["load_principal_acl"],
            "primary_load_tenant": workload_canaries[
                "primary_load_tenant"
            ],
            "primary_tenant": {
                "active_chunks": PRIMARY_TENANT_DOCUMENTS * CHUNKS_PER_VERSION,
                "documents": PRIMARY_TENANT_DOCUMENTS,
                "historical_chunks": (
                    PRIMARY_TENANT_DOCUMENTS
                    * (VERSIONS_PER_DOCUMENT - 1)
                    * CHUNKS_PER_VERSION
                ),
                "protected_active_chunks": (
                    primary_protected_documents * CHUNKS_PER_VERSION
                ),
                "public_active_chunks": (
                    primary_public_documents * CHUNKS_PER_VERSION
                ),
                "tenant_id": PRIMARY_TENANT_ID,
            },
            "protected_same_tenant_chunk_ids": workload_canaries[
                "protected_same_tenant_chunk_ids"
            ],
            "tenants": tenants,
            "versions_per_document": VERSIONS_PER_DOCUMENT,
        },
        "dataset_id": DATASET_ID,
        "description": (
            "Compact deterministic synthetic production-reference load corpus."
        ),
        "embedding_profile": {
            "derivation": VECTOR_DERIVATION,
            "dimensions": EMBEDDING_DIMENSIONS,
            "embedding_space_id": EMBEDDING_SPACE_ID,
            "model": EMBEDDING_MODEL,
            "normalization": EMBEDDING_NORMALIZATION,
            "provider": EMBEDDING_PROVIDER,
            "quality_claim": "none",
            "revision": EMBEDDING_REVISION,
        },
        "generation": {
            "builder": "scripts/build_load_corpus.py",
            "builder_version": BUILDER_VERSION,
            "materialization": "canonical-jsonl-sort-keys-utf8-lf",
            "normalizer_signature": NORMALIZER_SIGNATURE,
            "parameters": parameters,
            "splitter_signature": SPLITTER_SIGNATURE,
            "text_template_version": TEXT_TEMPLATE_VERSION,
        },
        "graph_expectations": _graph_expectations(tenant_document_counts),
        "pipeline_profile": {
            "code_signature": CODE_SIGNATURE,
            "extractor_signature": EXTRACTOR_SIGNATURE,
            "normalizer_signature": NORMALIZER_SIGNATURE,
            "profile_id": PIPELINE_PROFILE_ID,
            "prompt_signature": PROMPT_SIGNATURE,
            "schema_signature": SCHEMA_SIGNATURE,
            "splitter_signature": SPLITTER_SIGNATURE,
        },
        "python_api": {
            "bundle_iterator": "iter_graph_record_bundles",
            "module": "scripts.build_load_corpus",
            "record_iterators": [
                "iter_documents",
                "iter_versions",
                "iter_snapshots",
                "iter_chunks",
                "iter_embeddings",
                "iter_entities",
                "iter_mentions",
            ],
            "selection": ["tenant_id", "active_only"],
        },
        "retrieval_workload": build_retrieval_workload(),
        "owner": "repository-maintainers",
        "samples": samples,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "streams": streams,
        "synthetic": True,
        "version": DATASET_VERSION,
        "warning": (
            "Generated load data is not factual, provider-quality, cost, or "
            "production-candidate evidence by itself."
        ),
    }


def expected_compact_files(
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, bytes]:
    value = dict(manifest or build_manifest())
    return {
        "NOTICE.txt": NOTICE,
        "manifest.json": canonical_json_bytes(value),
    }


def _relative_files(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }


def check_compact_dataset(
    directory: Path = DEFAULT_DATASET_DIR,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    expected = expected_compact_files(manifest)
    if not directory.is_dir():
        return (f"compact load dataset directory is missing: {directory}",)
    errors: list[str] = []
    actual_paths = _relative_files(directory)
    if actual_paths != COMPACT_FILES:
        errors.append(
            "compact load dataset files differ: "
            f"missing={sorted(COMPACT_FILES - actual_paths)}, "
            f"extra={sorted(actual_paths - COMPACT_FILES)}"
        )
    for relative, payload in expected.items():
        path = directory / relative
        if not path.is_file():
            continue
        if path.read_bytes() != payload:
            errors.append(f"compact load dataset drifted: {relative}")
    return tuple(errors)


def write_compact_dataset(
    directory: Path = DEFAULT_DATASET_DIR,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    expected = expected_compact_files(manifest)
    if directory.exists():
        extras = _relative_files(directory) - COMPACT_FILES
        if extras:
            raise ValueError(
                "refusing to overwrite unknown compact load files: "
                + ", ".join(sorted(extras))
            )
    directory.mkdir(parents=True, exist_ok=True)
    for relative, payload in expected.items():
        (directory / relative).write_bytes(payload)


def materialize_dataset(directory: Path) -> dict[str, Any]:
    """Atomically stream the expanded corpus into a new external directory."""

    target = directory.resolve()
    if target == ROOT or ROOT in target.parents:
        raise ValueError(
            "materialization target must be outside the repository: "
            f"{target}"
        )
    if target.exists():
        raise ValueError(f"materialization target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent)
    )
    manifest = build_manifest()
    try:
        document_hash = hashlib.sha256()
        chunk_hash = hashlib.sha256()
        entity_hash = hashlib.sha256()
        mention_hash = hashlib.sha256()
        document_records = 0
        chunk_records = 0
        entity_records = 0
        mention_records = 0
        document_bytes = 0
        chunk_bytes = 0
        entity_bytes = 0
        mention_bytes = 0
        with (temporary / "documents.jsonl").open("wb") as documents_handle, (
            temporary / "chunks.jsonl"
        ).open("wb") as chunks_handle, (temporary / "entities.jsonl").open(
            "wb"
        ) as entities_handle, (temporary / "mentions.jsonl").open(
            "wb"
        ) as mentions_handle:
            for bundle in iter_version_bundles():
                document_payload = canonical_json_bytes(bundle.document)
                documents_handle.write(document_payload)
                document_hash.update(document_payload)
                document_records += 1
                document_bytes += len(document_payload)
                if bundle.document["version_number"] == 1:
                    entity_payload = canonical_json_bytes(bundle.entity)
                    entities_handle.write(entity_payload)
                    entity_hash.update(entity_payload)
                    entity_records += 1
                    entity_bytes += len(entity_payload)
                for chunk in bundle.chunks:
                    chunk_payload = canonical_json_bytes(chunk)
                    chunks_handle.write(chunk_payload)
                    chunk_hash.update(chunk_payload)
                    chunk_records += 1
                    chunk_bytes += len(chunk_payload)
                for mention in bundle.mentions:
                    mention_payload = canonical_json_bytes(mention)
                    mentions_handle.write(mention_payload)
                    mention_hash.update(mention_payload)
                    mention_records += 1
                    mention_bytes += len(mention_payload)
        actual_streams = {
            "documents": StreamSummary(
                document_records, document_bytes, document_hash.hexdigest()
            ).as_dict("documents.jsonl"),
            "chunks": StreamSummary(
                chunk_records, chunk_bytes, chunk_hash.hexdigest()
            ).as_dict("chunks.jsonl"),
            "entities": StreamSummary(
                entity_records, entity_bytes, entity_hash.hexdigest()
            ).as_dict("entities.jsonl"),
            "mentions": StreamSummary(
                mention_records, mention_bytes, mention_hash.hexdigest()
            ).as_dict("mentions.jsonl"),
        }
        if actual_streams != manifest["streams"]:
            raise RuntimeError("materialized load streams differ from manifest")
        (temporary / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        (temporary / "NOTICE.txt").write_bytes(NOTICE)
        temporary.rename(target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def _file_summary(path: Path) -> StreamSummary:
    digest = hashlib.sha256()
    size = 0
    records = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            size += len(line)
            if line.strip():
                records += 1
    return StreamSummary(records=records, size_bytes=size, sha256=digest.hexdigest())


def check_materialized_dataset(directory: Path) -> tuple[str, ...]:
    manifest = build_manifest()
    if not directory.is_dir():
        return (f"materialized load dataset directory is missing: {directory}",)
    errors: list[str] = []
    actual_paths = _relative_files(directory)
    if actual_paths != MATERIALIZED_FILES:
        errors.append(
            "materialized load dataset files differ: "
            f"missing={sorted(MATERIALIZED_FILES - actual_paths)}, "
            f"extra={sorted(actual_paths - MATERIALIZED_FILES)}"
        )
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file() and manifest_path.read_bytes() != canonical_json_bytes(
        manifest
    ):
        errors.append("materialized load manifest drifted")
    notice_path = directory / "NOTICE.txt"
    if notice_path.is_file() and notice_path.read_bytes() != NOTICE:
        errors.append("materialized load notice drifted")
    for role in manifest["streams"]:
        path = directory / manifest["streams"][role]["path"]
        if not path.is_file():
            continue
        actual = _file_summary(path).as_dict(path.name)
        if actual != manifest["streams"][role]:
            errors.append(f"materialized load stream drifted: {path.name}")
    return tuple(errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--write-compact", action="store_true")
    action.add_argument("--print-manifest", action="store_true")
    action.add_argument("--materialize", type=Path)
    action.add_argument("--check-materialized", type=Path)
    args = parser.parse_args()

    if args.check:
        errors = check_compact_dataset()
        if errors:
            raise SystemExit("; ".join(errors))
        manifest = build_manifest()
        print(
            "verified compact load-v1: "
            f"{manifest['counts']['active_chunks']} active / "
            f"{manifest['counts']['total_chunks']} total Chunks"
        )
    elif args.write_compact:
        write_compact_dataset()
        print(f"wrote compact load-v1 manifest to {DEFAULT_DATASET_DIR}")
    elif args.print_manifest:
        print(canonical_json_bytes(build_manifest()).decode("utf-8"), end="")
    elif args.materialize is not None:
        manifest = materialize_dataset(args.materialize)
        print(
            f"materialized {manifest['counts']['total_chunks']} Chunks to "
            f"{args.materialize}"
        )
    elif args.check_materialized is not None:
        errors = check_materialized_dataset(args.check_materialized)
        if errors:
            raise SystemExit("; ".join(errors))
        print(f"verified materialized load-v1 at {args.check_materialized}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
