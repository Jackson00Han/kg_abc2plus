"""Resolve industrial facets to bounded, authorized current Version IDs.

This adapter narrows the existing retrieval engine. It does not rank sources,
infer product applicability from prose, or turn graph paths into evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any

from neo4j import unit_of_work
from neo4j.exceptions import DriverError, Neo4jError

from graphrag_prod.domain import Principal
from graphrag_prod.retrieval.engine import (
    RetrievalBackendTimeout, RetrievalBackendUnavailable, RetrievalUnavailable,
)
from graphrag_prod.retrieval.models import VersionFilter


FAMILIES = ("canalis-kt", "evopact-hvx-up24")
SOURCE_KINDS = ("CURATED_REFERENCE", "OFFICIAL_PUBLICATION", "SYNTHETIC_FIELD_RECORD")
MAX_SCOPE_DOCUMENTS = 100
_ASSET_KEY = re.compile(r"^[a-z][a-z0-9-]{1,99}$")


@dataclass(frozen=True, slots=True)
class IndustrialScope:
    family: str | None = None
    asset_keys: tuple[str, ...] = ()
    include_references: bool = True
    source_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.family is not None and self.family not in FAMILIES:
            raise ValueError("industrial family is unsupported")
        if type(self.include_references) is not bool:
            raise TypeError("include_references must be boolean")
        for name, values, limit in (
            ("asset_keys", self.asset_keys, 8), ("source_kinds", self.source_kinds, 3),
        ):
            if (
                not isinstance(values, tuple) or len(values) > limit
                or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{name} must be a bounded unique tuple")
            object.__setattr__(self, name, tuple(sorted(values)))
        if any(_ASSET_KEY.fullmatch(key) is None for key in self.asset_keys):
            raise ValueError("industrial asset keys must be canonical identifiers")
        if any(kind not in SOURCE_KINDS for kind in self.source_kinds):
            raise ValueError("industrial source kind is unsupported")


@dataclass(frozen=True, slots=True)
class IndustrialScopeTrace:
    requested: IndustrialScope
    matched_documents: int
    reference_documents: int
    match_none: bool
    # No hidden-source counts or missing-asset identities are exposed.
    reference_only_sources_omitted: bool = True
    policy_version: str = "industrial-current-scope:v1"

    def __post_init__(self) -> None:
        if not isinstance(self.requested, IndustrialScope):
            raise TypeError("scope trace requires the validated requested scope")
        if any(type(value) is not int or not 0 <= value <= MAX_SCOPE_DOCUMENTS
               for value in (self.matched_documents, self.reference_documents)):
            raise ValueError("scope trace counts must be bounded")
        if self.reference_documents > self.matched_documents:
            raise ValueError("reference count exceeds matched sources")
        if type(self.match_none) is not bool or self.match_none != (self.matched_documents == 0):
            raise ValueError("scope trace match-none state differs from its source count")
        if self.reference_only_sources_omitted is not True or self.policy_version != "industrial-current-scope:v1":
            raise ValueError("scope trace policy is unsupported")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IndustrialScopeResolution:
    version_filter: VersionFilter
    trace: IndustrialScopeTrace

    def __post_init__(self) -> None:
        if not isinstance(self.version_filter, VersionFilter) or not isinstance(self.trace, IndustrialScopeTrace):
            raise TypeError("scope resolution requires a VersionFilter and scope trace")
        if (self.version_filter.document_ids or self.version_filter.match_none != self.trace.match_none
                or len(self.version_filter.version_ids) != self.trace.matched_documents):
            raise ValueError("resolved current versions must agree with the scope trace")


def build_rerank_scope_context(asset_keys: tuple[str, ...]) -> str | None:
    """Render only an already supplied, canonical equipment scope, never a gold answer."""
    scope = IndustrialScope(asset_keys=asset_keys)
    if not scope.asset_keys:
        return None
    return "User-selected equipment scope: " + ", ".join(
        key.removeprefix("asset-").upper() for key in scope.asset_keys
    )


_STATE_QUERY = """
// industrial-scope:state
OPTIONAL MATCH (state:TenantCorpusState {tenant_id:$tenant_id})
RETURN state.corpus_revision AS corpus_revision
"""

_SOURCE_MATCH = """
MATCH (document:Document {tenant_id:$tenant_id})-[:ACTIVE_VERSION]->
      (version:DocumentVersion {tenant_id:$tenant_id})
MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
      tenant_id:$tenant_id,build_state:'PUBLISHED'})-[:INCLUDES_CHUNK]->
      (chunk:Chunk {tenant_id:$tenant_id})
MATCH (snapshot)-[:OF_VERSION]->(version)
WHERE version.document_id=document.document_id
  AND snapshot.document_id=document.document_id AND snapshot.version_id=version.version_id
  AND chunk.document_id=document.document_id AND chunk.version_id=version.version_id
  AND chunk.access_policy_id=document.access_policy_id
  AND chunk.access_policy_version=document.access_policy_version
  AND chunk.access_groups=document.access_groups
  AND any(g IN $groups WHERE g IN document.access_groups)
  AND any(g IN $groups WHERE g IN chunk.access_groups)
  AND version.industrial_provenance_json IS NOT NULL
  AND version.industrial_provenance_checksum IS NOT NULL
  AND version.industrial_family IN $families
  AND version.industrial_contract_family = CASE version.industrial_family
      WHEN 'canalis-kt' THEN 'CANALIS_KT'
      WHEN 'evopact-hvx-up24' THEN 'EVOPACT_HVX_UP_TO_24KV' END
  AND version.industrial_source_kind IN $source_kinds
  AND (size($document_ids)=0 OR document.document_id IN $document_ids)
  AND (size($version_ids)=0 OR version.version_id IN $version_ids)
  AND ($published_before IS NULL OR version.published_at <= $published_before)
"""

_RETURN_SOURCES = """
RETURN DISTINCT document.document_id AS document_id, version.version_id AS version_id,
       version.industrial_family AS family, version.industrial_asset_keys AS asset_keys,
       version.industrial_source_kind AS source_kind
ORDER BY document_id, version_id
LIMIT $limit
"""

ASSET_SCOPE_QUERY = "// industrial-scope:assets\n" + _SOURCE_MATCH + """
  AND version.industrial_source_kind='SYNTHETIC_FIELD_RECORD'
  AND any(key IN $asset_keys WHERE key IN version.industrial_asset_keys)
""" + _RETURN_SOURCES

SOURCE_SCOPE_QUERY = "// industrial-scope:sources\n" + _SOURCE_MATCH + """
  AND (
      (version.industrial_source_kind='SYNTHETIC_FIELD_RECORD'
       AND (size($asset_keys)=0 OR any(key IN $asset_keys WHERE key IN version.industrial_asset_keys)))
      OR ($include_references AND version.industrial_source_kind IN ['CURATED_REFERENCE','OFFICIAL_PUBLICATION']
          AND size(version.industrial_asset_keys)=0)
  )
""" + _RETURN_SOURCES


class _ScopeChanged(RuntimeError):
    pass


class Neo4jIndustrialScopeResolver:
    """Resolve at most 100 current sources with a bounded consistency retry.

An asset request first requires each requested identity to occur in an
authorized, current field source under the caller's Version filter. Family
references are considered only after that succeeds. Unknown, inaccessible and
scope-incompatible identities produce the same empty result.
"""

    def __init__(
        self, driver: Any, database: str = "neo4j", *, transaction_timeout_seconds: float = 15.0,
    ) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        if not isinstance(database, str) or not database.strip() or any(c in database for c in "\x00\r\n"):
            raise ValueError("database must be a non-empty identifier")
        if (
            isinstance(transaction_timeout_seconds, bool)
            or not isinstance(transaction_timeout_seconds, (int, float))
            or not math.isfinite(transaction_timeout_seconds)
            or not 0 < transaction_timeout_seconds <= 60
        ):
            raise ValueError("scope transaction timeout must be between zero and 60 seconds")
        self.driver = driver
        self.database = database
        self._work = unit_of_work(
            metadata={"component": "industrial-scope", "operation": "resolve"},
            timeout=float(transaction_timeout_seconds),
        )(self._resolve_tx)

    def resolve(
        self, principal: Principal, scope: IndustrialScope, *, version_filter: VersionFilter = VersionFilter(),
    ) -> IndustrialScopeResolution:
        if not isinstance(principal, Principal) or not isinstance(scope, IndustrialScope):
            raise TypeError("scope resolution requires a Principal and IndustrialScope")
        if not isinstance(version_filter, VersionFilter):
            raise TypeError("version_filter must be VersionFilter")
        if len(version_filter.document_ids) + len(version_filter.version_ids) > MAX_SCOPE_DOCUMENTS:
            raise ValueError("industrial version filter exceeds 100 IDs")
        if version_filter.match_none:
            return self._resolution(scope, version_filter, ())
        try:
            with self.driver.session(database=self.database) as session:
                for attempt in range(2):
                    try:
                        return session.execute_read(self._work, principal, scope, version_filter)
                    except _ScopeChanged:
                        if attempt:
                            raise RetrievalUnavailable("industrial source scope changed repeatedly") from None
        except Neo4jError as error:
            if "TimedOut" in str(getattr(error, "code", "")):
                raise RetrievalBackendTimeout() from error
            raise RetrievalBackendUnavailable() from error
        except DriverError as error:
            raise RetrievalBackendUnavailable() from error
        raise AssertionError("bounded scope attempts exhausted")

    @staticmethod
    def _rows(tx: Any, query: str, parameters: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        rows = tuple(dict(row) for row in tx.run(query, **parameters))
        if len(rows) > MAX_SCOPE_DOCUMENTS:
            raise RetrievalUnavailable("industrial source scope exceeds 100 documents; narrow the scope")
        for row in rows:
            if (
                row.get("family") not in FAMILIES or row.get("source_kind") not in SOURCE_KINDS
                or not isinstance(row.get("asset_keys"), list)
                or len(row["asset_keys"]) > 32
                or any(not isinstance(key, str) or _ASSET_KEY.fullmatch(key) is None for key in row["asset_keys"])
                or row["asset_keys"] != sorted(set(row["asset_keys"]))
                or any(not isinstance(row.get(key), str) or not row[key] or len(row[key]) > 256
                       for key in ("document_id", "version_id"))
            ):
                raise RetrievalUnavailable("industrial source facets are invalid")
        if len({row["document_id"] for row in rows}) != len(rows):
            raise RetrievalUnavailable("industrial sources have inconsistent current versions")
        if len({row["version_id"] for row in rows}) != len(rows):
            raise RetrievalUnavailable("industrial source versions are not unique")
        return rows

    @classmethod
    def _resolve_tx(
        cls, tx: Any, principal: Principal, scope: IndustrialScope, version_filter: VersionFilter,
    ) -> IndustrialScopeResolution:
        parameters = {
            "tenant_id": principal.tenant_id, "groups": sorted(principal.groups),
            "families": [scope.family] if scope.family else list(FAMILIES),
            "asset_keys": list(scope.asset_keys), "include_references": scope.include_references,
            "source_kinds": list(scope.source_kinds or SOURCE_KINDS),
            "document_ids": sorted(version_filter.document_ids),
            "version_ids": sorted(version_filter.version_ids),
            "published_before": version_filter.published_at_or_before,
            "limit": MAX_SCOPE_DOCUMENTS + 1,
        }
        start = tuple(dict(row) for row in tx.run(_STATE_QUERY, tenant_id=principal.tenant_id))
        if len(start) != 1:
            raise RetrievalUnavailable("industrial corpus state is invalid")
        rows: tuple[dict[str, Any], ...] = ()
        if scope.asset_keys:
            assets = cls._rows(tx, ASSET_SCOPE_QUERY, {**parameters, "source_kinds": list(SOURCE_KINDS)})
            resolved = {key for row in assets for key in row["asset_keys"]}
            if set(scope.asset_keys) <= resolved:
                # Only authorized primary identities determine applicable families.
                parameters["families"] = sorted({row["family"] for row in assets})
                rows = cls._rows(tx, SOURCE_SCOPE_QUERY, parameters)
        else:
            rows = cls._rows(tx, SOURCE_SCOPE_QUERY, parameters)
        end = tuple(dict(row) for row in tx.run(_STATE_QUERY, tenant_id=principal.tenant_id))
        if end != start:
            raise _ScopeChanged()
        return cls._resolution(scope, version_filter, rows)

    @staticmethod
    def _resolution(
        scope: IndustrialScope, original: VersionFilter, rows: tuple[dict[str, Any], ...],
    ) -> IndustrialScopeResolution:
        effective = VersionFilter(
            # IDs were intersected in Cypher; Version IDs also prevent a later
            # active-version change from widening a previously resolved scope.
            version_ids=frozenset(row["version_id"] for row in rows),
            published_at_or_before=original.published_at_or_before,
            match_none=not rows,
        )
        trace = IndustrialScopeTrace(
            scope, len(rows), sum(row["source_kind"] != "SYNTHETIC_FIELD_RECORD" for row in rows),
            not rows,
        )
        return IndustrialScopeResolution(effective, trace)
