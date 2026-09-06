"""Bounded current-source inventory for ordinary authorized industrial readers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import time
from neo4j import Query

from graphrag_prod.domain import Principal
from graphrag_prod.retrieval.engine import RetrievalUnavailable
from .provenance import Neo4jIndustrialProvenanceStore
from .retrieval import IndustrialScope, Neo4jIndustrialScopeResolver, _SOURCE_MATCH, SOURCE_KINDS, FAMILIES


def _run(session: Any, query: str, **parameters: Any):
    return session.run(Query(query, timeout=2.0), **parameters)


def _deadline(start: float) -> None:
    if time.monotonic() - start > 15.0:
        raise RetrievalUnavailable("industrial source read exceeded its deadline")


@dataclass(frozen=True, slots=True)
class IndustrialSourceSummary:
    document_id: str
    version_id: str
    title: str
    source_kind: str
    family: str
    asset_keys: tuple[str, ...]
    published_at: datetime | None
    chunk_count: int
    first_chunk_id: str
    canonical_uri: str | None = None


@dataclass(frozen=True, slots=True)
class IndustrialSourcePage:
    sources: tuple[IndustrialSourceSummary, ...]
    has_more: bool


class Neo4jIndustrialSourceCatalog:
    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver, self.database = driver, database
        self.resolver = Neo4jIndustrialScopeResolver(driver, database)
        self.provenance = Neo4jIndustrialProvenanceStore(driver, database)

    def _revision(self, principal: Principal) -> int | None:
        with self.driver.session(database=self.database) as session:
            row = _run(session,"OPTIONAL MATCH (s:TenantCorpusState {tenant_id:$tenant}) "
                              "RETURN s.corpus_revision AS revision", tenant=principal.tenant_id).single()
        return None if row is None else row["revision"]

    def list(self, principal: Principal, *, family: str | None = None,
             asset_keys: tuple[str, ...] = (), limit: int = 50) -> IndustrialSourcePage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("source list limit must be between 1 and 100")
        if "retrieval:read" not in principal.capabilities:
            raise PermissionError("source inventory requires retrieval:read")
        started = time.monotonic()
        before = self._revision(principal)
        resolution = self.resolver.resolve(principal, IndustrialScope(family=family, asset_keys=asset_keys))
        if resolution.version_filter.match_none:
            return IndustrialSourcePage((), False)
        query = _SOURCE_MATCH + """
                WITH document, version, chunk ORDER BY chunk.ordinal, chunk.chunk_id
                WITH document, version, count(DISTINCT chunk) AS chunk_count,
                     collect(chunk.chunk_id)[0] AS first_chunk_id
                RETURN document.document_id AS document_id,version.version_id AS version_id,
                       document.title AS title,document.canonical_uri AS canonical_uri,version.published_at AS published_at,
                       version.industrial_family AS family,version.industrial_source_kind AS source_kind,
                       version.industrial_asset_keys AS asset_keys,chunk_count,first_chunk_id,
                       version.industrial_provenance_checksum AS provenance_checksum,
                       version.industrial_source_key AS source_key,
                       version.checksum AS normalized_checksum,version.original_checksum AS original_checksum,
                       document.access_policy_id AS access_policy_id,
                       document.access_policy_version AS access_policy_version,
                       document.access_groups AS access_groups
                ORDER BY document_id,version_id LIMIT $limit
                """
        parameters = dict(tenant_id=principal.tenant_id, groups=sorted(principal.groups),
            families=list(FAMILIES), source_kinds=list(SOURCE_KINDS), document_ids=[],
            version_ids=sorted(resolution.version_filter.version_ids), published_before=None, limit=limit + 1)
        with self.driver.session(database=self.database) as session:
            rows = tuple(dict(row) for row in _run(session, query, **parameters))
        result = []
        # Read one bounded immutable map at a time, never 100 eight-MiB maps together.
        for row in rows[:limit]:
            _deadline(started)
            metadata = self.provenance.for_chunk(principal, row["first_chunk_id"])
            if (metadata is None or any(metadata.get(key) != row[key] for key in
                                       ("document_id", "version_id", "family", "source_kind", "asset_keys",
                                        "access_policy_id", "access_policy_version", "access_groups",
                                        "source_key", "normalized_checksum", "original_checksum"))
                    or metadata.get("source_provenance_checksum") != row["provenance_checksum"]):
                raise RetrievalUnavailable("industrial source inventory changed or provenance differs")
            published_at = row["published_at"]
            if hasattr(published_at, "to_native"):
                published_at = published_at.to_native()
            result.append(IndustrialSourceSummary(
                row["document_id"], row["version_id"], row["title"], row["source_kind"],
                row["family"], tuple(row["asset_keys"]), published_at,
                int(row["chunk_count"]), row["first_chunk_id"], row["canonical_uri"],
            ))
        _deadline(started)
        with self.driver.session(database=self.database) as session:
            final_rows = tuple(dict(row) for row in _run(session, query, **parameters))
        if final_rows != rows:
            raise RetrievalUnavailable("industrial source inventory authorization changed during read")
        if before != self._revision(principal):
            raise RetrievalUnavailable("industrial source inventory changed during read")
        return IndustrialSourcePage(tuple(result), len(rows) > limit)

    def chunk(self, principal: Principal, *, chunk_id: str) -> IndustrialSourceChunk | None:
        from graphrag_prod.domain.ids import content_checksum
        from .provenance import canonical_json
        if "retrieval:read" not in principal.capabilities:
            raise PermissionError("source evidence requires retrieval:read")
        if not isinstance(chunk_id, str) or not 1 <= len(chunk_id) <= 256:
            raise ValueError("source chunk identifier is invalid")
        started = time.monotonic()
        before = self._revision(principal)
        metadata = self.provenance.for_chunk(principal, chunk_id)
        if metadata is None or metadata.get("family") not in FAMILIES:
            return None
        with self.driver.session(database=self.database) as session:
            row = _run(session,_SOURCE_MATCH + """
                AND chunk.chunk_id=$chunk_id
                AND chunk.char_start>=0 AND chunk.char_end>chunk.char_start
                AND chunk.char_end<=size(version.normalized_text)
                AND chunk.text=substring(version.normalized_text,chunk.char_start,chunk.char_end-chunk.char_start)
                RETURN document.document_id AS document_id,version.version_id AS version_id,
                       document.title AS title,document.canonical_uri AS canonical_uri,version.published_at AS published_at,
                       version.published_at.epochSeconds AS published_epoch_seconds,
                       version.published_at.nanosecond AS published_nanosecond,
                       version.industrial_family AS family,version.industrial_source_kind AS source_kind,
                       version.industrial_asset_keys AS asset_keys,
                       version.industrial_provenance_checksum AS provenance_checksum,
                       version.industrial_source_key AS source_key,
                       version.checksum AS normalized_checksum,version.original_checksum AS original_checksum,
                       document.access_policy_id AS access_policy_id,document.access_policy_version AS access_policy_version,
                       document.access_groups AS access_groups,
                       CASE WHEN size(chunk.text)<=50000 THEN chunk.text ELSE null END AS text,
                       chunk.checksum AS checksum,chunk.ordinal AS ordinal,
                       chunk.char_start AS char_start,chunk.char_end AS char_end,
                       chunk.page_number AS page_number,chunk.section AS section
                """, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                families=list(FAMILIES), source_kinds=list(SOURCE_KINDS), document_ids=[],
                version_ids=[metadata["version_id"]], published_before=None, chunk_id=chunk_id).single()
        if row is None:
            raise RetrievalUnavailable("industrial source evidence changed during read")
        row = dict(row)
        if (not isinstance(row["text"], str) or content_checksum(row["text"]) != row["checksum"]
                or row["char_end"] - row["char_start"] != len(row["text"])
                or any(metadata.get(key) != row[key] for key in
                       ("document_id", "version_id", "family", "source_kind", "asset_keys",
                        "access_policy_id", "access_policy_version", "access_groups",
                                        "source_key", "normalized_checksum", "original_checksum"))
                or metadata.get("source_provenance_checksum") != row["provenance_checksum"]
                or metadata.get("chunk_page_number") != row["page_number"]
                or metadata.get("chunk_ordinal") != row["ordinal"]
                or metadata.get("chunk_section") != row["section"]
                or metadata["chunk_char_start"] != row["char_start"]
                or metadata["chunk_char_end"] != row["char_end"]):
            raise RetrievalUnavailable("industrial source evidence identity or exact range differs")
        # The summary's count and first ID refer to the complete authorized current source.
        with self.driver.session(database=self.database) as session:
            summary = _run(session,_SOURCE_MATCH + """
                AND document.access_policy_id=$policy_id AND document.access_policy_version=$policy_version
                AND document.access_groups=$access_groups AND version.industrial_provenance_checksum=$map_checksum
                AND version.industrial_family=$family AND version.industrial_asset_keys=$asset_keys
                AND version.industrial_source_kind=$source_kind AND version.industrial_source_key=$source_key
                AND document.title=$title AND document.canonical_uri=$canonical_uri
                AND version.checksum=$normalized_checksum AND version.original_checksum=$original_checksum
                AND ((version.published_at.epochSeconds=$published_epoch_seconds
                      AND version.published_at.nanosecond=$published_nanosecond)
                     OR (version.published_at IS NULL AND $published_epoch_seconds IS NULL AND $published_nanosecond IS NULL))
                WITH chunk, version ORDER BY chunk.ordinal,chunk.chunk_id
                RETURN count(DISTINCT chunk) AS chunk_count,collect(chunk.chunk_id)[0] AS first_chunk_id,
                       max(CASE WHEN chunk.ordinal=$ordinal-1 THEN chunk.chunk_id END) AS previous_chunk_id,
                       max(CASE WHEN chunk.ordinal=$ordinal+1 THEN chunk.chunk_id END) AS next_chunk_id,
                       max(CASE WHEN chunk.chunk_id=$chunk_id AND chunk.checksum=$checksum
                             AND chunk.char_start=$char_start AND chunk.char_end=$char_end
                             AND chunk.ordinal=$ordinal AND chunk.text=$text
                             AND chunk.char_end<=size(version.normalized_text)
                             AND chunk.text=substring(version.normalized_text,chunk.char_start,chunk.char_end-chunk.char_start)
                             AND (chunk.page_number=$page_number OR (chunk.page_number IS NULL AND $page_number IS NULL))
                             AND (chunk.section=$section OR (chunk.section IS NULL AND $section IS NULL))
                           THEN chunk.chunk_id END) AS authorized_target
                """, tenant_id=principal.tenant_id, groups=sorted(principal.groups),
                families=list(FAMILIES), source_kinds=list(SOURCE_KINDS), document_ids=[],
                version_ids=[metadata["version_id"]], published_before=None, ordinal=row["ordinal"],
                policy_id=metadata["access_policy_id"], policy_version=metadata["access_policy_version"],
                access_groups=metadata["access_groups"], map_checksum=metadata["source_provenance_checksum"],
                family=metadata["family"], asset_keys=metadata["asset_keys"], source_kind=metadata["source_kind"], source_key=metadata["source_key"],
                chunk_id=chunk_id, checksum=row["checksum"], char_start=row["char_start"], char_end=row["char_end"],
                page_number=row["page_number"], section=row["section"], text=row["text"],
                title=row["title"], canonical_uri=row["canonical_uri"],
                published_epoch_seconds=row["published_epoch_seconds"], published_nanosecond=row["published_nanosecond"],
                normalized_checksum=metadata["normalized_checksum"], original_checksum=metadata["original_checksum"]).single()
        _deadline(started)
        if (summary is None or summary["chunk_count"] == 0 or summary["authorized_target"] != chunk_id
                or before != self._revision(principal)):
            raise RetrievalUnavailable("industrial source evidence changed during read")
        published_at = row["published_at"]
        if hasattr(published_at, "to_native"):
            published_at = published_at.to_native()
        source = IndustrialSourceSummary(row["document_id"], row["version_id"], row["title"],
            row["source_kind"], row["family"], tuple(row["asset_keys"]), published_at,
            int(summary["chunk_count"]), summary["first_chunk_id"], row["canonical_uri"])
        provenance = {key: metadata.get(key) for key in (
            "original_checksum", "normalized_checksum", "source_id", "source_kind", "source_origin",
            "source_locations", "selected_pages",
        )}
        official_source = metadata.get("source")
        if isinstance(official_source, dict):
            provenance["source_id"] = official_source.get("source_id")
            provenance["source"] = {key: official_source.get(key) for key in (
                "source_id", "portal_url", "download_url", "document_reference", "embedded_revision", "applicability",
            )}
        if len(canonical_json(provenance).encode("utf-8")) > 512 * 1024:
            raise RetrievalUnavailable("industrial source location output exceeds its byte limit")
        return IndustrialSourceChunk(source, chunk_id, row["text"], row["checksum"], row["ordinal"],
            row["char_start"], row["char_end"], row["page_number"], row["section"], provenance,
            summary["previous_chunk_id"], summary["next_chunk_id"])


@dataclass(frozen=True, slots=True)
class IndustrialSourceChunk:
    source: IndustrialSourceSummary
    chunk_id: str
    text: str
    checksum: str
    ordinal: int
    char_start: int
    char_end: int
    page_number: int | None
    section: str | None
    provenance: dict[str, Any]
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
