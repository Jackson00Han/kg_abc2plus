"""Immutable industrial source maps on version nodes, with current-source ACLs."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from graphrag_prod.domain.access import Principal

from .corpus import FAMILY_TO_CONTRACT


MAX_PROVENANCE_BYTES = 8 * 1024 * 1024


class IndustrialProvenanceConflict(ValueError):
    """Source identity, immutable metadata or current authorization conflicts."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _source_facets(metadata: dict[str, Any]) -> dict[str, Any]:
    """Derive bounded query fields from the same immutable canonical payload."""
    family = metadata.get("family")
    contract_family = metadata.get("contract_family")
    asset_keys = metadata.get("asset_keys")
    if family is not None and family not in FAMILY_TO_CONTRACT:
        raise IndustrialProvenanceConflict("industrial source family is invalid")
    if contract_family != FAMILY_TO_CONTRACT.get(family):
        raise IndustrialProvenanceConflict("industrial source contract family differs")
    if (not isinstance(asset_keys, list) or len(asset_keys) > 32
            or any(not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9-]{1,99}", key) is None for key in asset_keys)
            or asset_keys != sorted(set(asset_keys))):
        raise IndustrialProvenanceConflict("industrial source asset keys must be bounded and canonical")
    kind = metadata.get("source_kind")
    key = metadata.get("source_key")
    if kind not in {"CURATED_REFERENCE", "SYNTHETIC_FIELD_RECORD", "OFFICIAL_PUBLICATION"}:
        raise IndustrialProvenanceConflict("industrial source kind is invalid")
    if not isinstance(key, str) or not key or len(key) > 256 or any(ord(char) < 32 for char in key):
        raise IndustrialProvenanceConflict("industrial source key is invalid")
    return {"family": family, "contract_family": contract_family, "asset_keys": asset_keys,
            "source_kind": kind, "source_key": key}


class Neo4jIndustrialProvenanceStore:
    """Keep maps with DocumentVersion so normal version deletion removes them.

    There is intentionally no unauthenticated or historical fallback reader.
    The source map stores metadata only; exact text remains in the version and
    its chunks. Current document and chunk permissions are checked in Cypher.
    """

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self.driver = driver
        self.database = database

    def persist(self, principal: Principal, metadata: dict[str, Any]) -> None:
        if "knowledge:import" not in principal.capabilities:
            raise PermissionError("industrial source provenance requires knowledge:import")
        if metadata.get("tenant_id") != principal.tenant_id:
            raise IndustrialProvenanceConflict("source provenance tenant differs")
        _source_facets(metadata)
        encoded = canonical_json(metadata)
        if len(encoded.encode("utf-8")) > MAX_PROVENANCE_BYTES:
            raise IndustrialProvenanceConflict("source provenance exceeds byte limit")
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._persist_tx, principal, metadata, encoded)

    @staticmethod
    def _persist_tx(tx: Any, principal: Principal, metadata: dict[str, Any], encoded: str) -> None:
        row = tx.run(
            """
            MATCH (document:Document {tenant_id:$tenant, document_id:$document_id})
            SET document.__industrial_provenance_lock = randomUUID()
            WITH document REMOVE document.__industrial_provenance_lock
            WITH document
            MATCH (document)-[:ACTIVE_VERSION]->(version:DocumentVersion {
                tenant_id:$tenant, version_id:$version_id})
            WHERE version.document_id = document.document_id
              AND version.checksum = $normalized_checksum
              AND version.original_checksum = $original_checksum
              AND document.access_policy_id = $access_policy_id
              AND document.access_policy_version = $access_policy_version
              AND document.access_groups = $access_groups
              AND all(g IN document.access_groups WHERE g IN $groups)
            RETURN version.industrial_provenance_json AS existing,
                   {family:version.industrial_family,contract_family:version.industrial_contract_family,
                    asset_keys:version.industrial_asset_keys,source_kind:version.industrial_source_kind,
                    source_key:version.industrial_source_key} AS facets
            """,
            tenant=principal.tenant_id, groups=sorted(principal.groups),
            **{key: metadata[key] for key in (
                "document_id", "version_id", "normalized_checksum", "original_checksum",
                "access_policy_id", "access_policy_version", "access_groups",
            )},
        ).single()
        if row is None:
            raise IndustrialProvenanceConflict("source provenance target is unavailable or incompatible")
        facets = _source_facets(metadata)
        if row["existing"] is not None:
            if row["existing"] != encoded or dict(row["facets"]) != facets:
                raise IndustrialProvenanceConflict("immutable source provenance or facets differ")
        elif any(value is not None for value in dict(row["facets"]).values()):
            raise IndustrialProvenanceConflict("source facets exist without immutable provenance")
        tx.run(
            "MATCH (version:DocumentVersion {tenant_id:$tenant,version_id:$version_id}) "
            "SET version.industrial_provenance_json=$payload, version.industrial_provenance_checksum=$checksum, "
            "version.industrial_family=$facets.family,version.industrial_contract_family=$facets.contract_family, "
            "version.industrial_asset_keys=$facets.asset_keys,version.industrial_source_kind=$facets.source_kind, "
            "version.industrial_source_key=$facets.source_key",
            tenant=principal.tenant_id, version_id=metadata["version_id"],
            payload=encoded, checksum=hashlib.sha256(encoded.encode("utf-8")).hexdigest(), facets=facets,
        ).consume()

    def for_chunk(self, principal: Principal, chunk_id: str) -> dict[str, Any] | None:
        with self.driver.session(database=self.database) as session:
            row = session.run(
                """
                MATCH (document:Document {tenant_id:$tenant})-[:ACTIVE_VERSION]->
                      (version:DocumentVersion {tenant_id:$tenant})-[:HAS_CHUNK]->
                      (chunk:Chunk {tenant_id:$tenant,chunk_id:$chunk_id})
                MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
                      tenant_id:$tenant,build_state:'PUBLISHED'})-[:INCLUDES_CHUNK]->(chunk)
                MATCH (snapshot)-[:OF_VERSION]->(version)
                WHERE any(g IN $groups WHERE g IN document.access_groups)
                  AND any(g IN $groups WHERE g IN chunk.access_groups)
                  AND chunk.document_id=document.document_id AND chunk.version_id=version.version_id
                  AND chunk.access_policy_id=document.access_policy_id
                  AND chunk.access_policy_version=document.access_policy_version
                  AND chunk.access_groups=document.access_groups
                  AND snapshot.document_id=document.document_id AND snapshot.version_id=version.version_id
                  AND version.industrial_provenance_json IS NOT NULL
                RETURN version.industrial_provenance_json AS payload,
                       version.industrial_provenance_checksum AS checksum,
                       {family:version.industrial_family,contract_family:version.industrial_contract_family,
                        asset_keys:version.industrial_asset_keys,source_kind:version.industrial_source_kind,
                        source_key:version.industrial_source_key} AS facets,
                       version.checksum AS normalized_checksum,version.original_checksum AS original_checksum,
                       version.version_id AS version_id, document.document_id AS document_id,
                       document.access_policy_id AS access_policy_id,document.access_policy_version AS access_policy_version,
                       document.access_groups AS access_groups,
                       chunk.char_start AS char_start,chunk.char_end AS char_end,
                       chunk.page_number AS page_number
                """,
                tenant=principal.tenant_id, chunk_id=chunk_id, groups=sorted(principal.groups),
            ).single()
        if row is None:
            return None
        encoded = row["payload"]
        if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_PROVENANCE_BYTES:
            raise IndustrialProvenanceConflict("stored source provenance exceeds safe bounds")
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["checksum"]:
            raise IndustrialProvenanceConflict("stored source provenance checksum differs")
        try:
            metadata = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise IndustrialProvenanceConflict("stored source provenance is invalid") from exc
        if not isinstance(metadata, dict) or _source_facets(metadata) != dict(row["facets"]):
            raise IndustrialProvenanceConflict("stored source provenance facets differ")
        for key in ("version_id", "document_id", "normalized_checksum", "original_checksum",
                    "access_policy_id", "access_policy_version", "access_groups"):
            if metadata.get(key) != row[key]:
                raise IndustrialProvenanceConflict("stored source provenance identity differs")
        if metadata.get("tenant_id") != principal.tenant_id:
            raise IndustrialProvenanceConflict("stored source provenance tenant differs")
        locations = [
            item for item in metadata.get("source_locations", ())
            if item["page_number"] == row["page_number"]
            and item["char_start"] < row["char_end"] and item["char_end"] > row["char_start"]
        ]
        return {
            **metadata, "source_locations": locations, "chunk_id": chunk_id,
            "chunk_char_start": row["char_start"], "chunk_char_end": row["char_end"],
        }
