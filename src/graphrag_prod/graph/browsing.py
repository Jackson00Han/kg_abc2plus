"""Permission-safe, version-pinned browsing of a bounded governed publication.

The existing exact-evidence projector remains the record validator. Browsing
adds an entity entry point, a caller-specific bounded view and deterministic
pagination; it does not reinterpret materialized edges as factual evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from typing import Any

from neo4j import unit_of_work

from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import content_checksum
from graphrag_prod.retrieval.models import VersionFilter
from graphrag_prod.retrieval.subgraph import (
    EvidenceSubgraphLimits, Neo4jEvidenceSubgraphProjector, SubgraphTrustPolicy,
    _ASSERTION_QUERY, _MENTION_QUERY,
)

from .browse_models import (
    GRAPH_READ_CAPABILITY, MAX_GRAPH_EDGES, MAX_GRAPH_NODES, MAX_GRAPH_RECORDS,
    GraphBrowseError, GraphBrowseLimitExceeded, GraphBrowseQuery,
    GraphBrowseUnavailable, GraphReadPin, GraphViewChanged, read_graph_state,
)
from .view_tokens import GraphViewTokenCodec, digest


_SOURCE_METADATA = """RETURN version.industrial_source_kind AS source_kind,
       version.industrial_provenance_json AS source_metadata,
       version.industrial_provenance_checksum AS source_metadata_checksum,
       publication.publication_id AS publication_id,"""
# These fixed transformations change only the entry point. All authorization,
# active source lifecycle, T-Box, endpoint and exact-range predicates stay in
# the shared projector queries. No client text is interpolated into Cypher.
_BROWSE_MENTIONS = _MENTION_QUERY.replace(
    "WHERE chunk.chunk_id IN $chunk_ids", "WHERE publication.publication_id = $publication_id"
).replace("RETURN publication.publication_id AS publication_id,", _SOURCE_METADATA)
_BROWSE_ASSERTIONS = _ASSERTION_QUERY.replace(
    "WHERE seed_chunk.chunk_id IN $chunk_ids", "WHERE publication.publication_id = $publication_id"
).replace("RETURN publication.publication_id AS publication_id,", _SOURCE_METADATA)


def _native(value: Any) -> Any:
    if hasattr(value, "to_native"):
        return _native(value.to_native())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    return value


def _filter_payload(value: VersionFilter) -> dict[str, Any]:
    return {"document_ids": sorted(value.document_ids), "version_ids": sorted(value.version_ids),
            "published_at_or_before": None if value.published_at_or_before is None else value.published_at_or_before.isoformat(),
            "match_none": value.match_none}


def _decode_filter(value: dict[str, Any]) -> VersionFilter:
    if set(value) != {"document_ids", "version_ids", "published_at_or_before", "match_none"}:
        raise GraphViewChanged()
    return VersionFilter(document_ids=frozenset(value["document_ids"]), version_ids=frozenset(value["version_ids"]),
        published_at_or_before=None if value["published_at_or_before"] is None else datetime.fromisoformat(value["published_at_or_before"]),
        match_none=value["match_none"])


@dataclass(frozen=True)
class _View:
    pin: GraphReadPin
    schema: dict[str, Any]
    nodes: dict[str, dict[str, Any]]
    assertions: dict[str, Any]
    evidence: dict[str, dict[str, Any]]
    view_digest: str


class Neo4jPublishedGraphBrowser:
    def __init__(self, driver: Any, database: str = "neo4j", *, cursor_signing_key: bytes | None = None,
                 token_codec: GraphViewTokenCodec | None = None, transaction_timeout_seconds: float = 30.0) -> None:
        if driver is None or not isinstance(database, str) or not database.strip():
            raise ValueError("graph browser requires a driver and database")
        if isinstance(transaction_timeout_seconds, bool) or not isinstance(transaction_timeout_seconds, (int, float)) or not 0 < transaction_timeout_seconds <= 60:
            raise ValueError("graph read timeout is outside its bound")
        self.driver, self.database = driver, database
        self.tokens = token_codec or GraphViewTokenCodec(cursor_signing_key)
        self._read_work = unit_of_work(timeout=float(transaction_timeout_seconds), metadata={"component": "governed-graph-browser"})(self._load_tx)

    @staticmethod
    def _authorize(principal: Principal) -> None:
        if not isinstance(principal, Principal) or GRAPH_READ_CAPABILITY not in principal.capabilities:
            raise PermissionError("graph read capability required")

    def _load(self, principal: Principal, policy: str, version_filter: VersionFilter) -> _View:
        try:
            with self.driver.session(database=self.database) as session:
                return session.execute_read(self._read_work, principal, SubgraphTrustPolicy(policy), version_filter)
        except (GraphBrowseError, PermissionError, TimeoutError):
            raise
        except Exception as error:
            raise GraphBrowseUnavailable() from error

    @staticmethod
    def _rows(tx: Any, parameters: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        mentions = [dict(row) for row in tx.run(_BROWSE_MENTIONS, **parameters, mention_limit=MAX_GRAPH_RECORDS + 1)]
        assertions = [dict(row) for row in tx.run(_BROWSE_ASSERTIONS, **parameters, assertion_limit=MAX_GRAPH_RECORDS + 1)]
        if len(mentions) + len(assertions) > MAX_GRAPH_RECORDS:
            raise GraphBrowseLimitExceeded()
        ids = [row["mention"]["revision_id"] for row in mentions] + [row["assertion"]["revision_id"] for row in assertions]
        if len(set(ids)) != len(ids):
            raise GraphViewChanged()
        chunks: dict[str, str] = {}
        for row in (*mentions, *assertions):
            citation = row["citation"]
            previous = chunks.setdefault(citation["chunk_id"], citation["chunk_text"])
            if previous != citation["chunk_text"]:
                raise GraphViewChanged()
        if sum(map(len, chunks.values())) > 500_000 or sum(len(str(row.get("source_metadata") or "")) for row in (*mentions, *assertions)) > 4_000_000:
            raise GraphBrowseLimitExceeded()
        return mentions, assertions

    @classmethod
    def _load_tx(cls, tx: Any, principal: Principal, policy: SubgraphTrustPolicy, version_filter: VersionFilter) -> _View:
        pin, tbox = read_graph_state(tx, principal.tenant_id)
        if pin.publication_id is None or version_filter.match_none:
            if read_graph_state(tx, principal.tenant_id)[0] != pin:
                raise GraphViewChanged()
            return _View(pin, {"entity_types": (), "relationship_types": (), "hierarchies": ()}, {}, {}, {}, digest([]))
        parameters = {"tenant_id": principal.tenant_id, "groups": sorted(principal.groups), "publication_id": pin.publication_id,
            "authority_levels": [item.value for item in policy.authority_levels], "max_chunk_chars": 50_000,
            "seed_entity_limit": MAX_GRAPH_RECORDS + 1, "document_ids": sorted(version_filter.document_ids),
            "version_ids": sorted(version_filter.version_ids), "published_before": version_filter.published_at_or_before}
        mentions, assertions = cls._rows(tx, parameters)
        view_digest = digest(_native([mentions, assertions]))
        # Neo4j read transactions are read-committed. Re-read authorization and
        # evidence membership before returning, including ACL changes that did
        # not advance the usual corpus revision (e.g. administrative repairs).
        if digest(_native(cls._rows(tx, parameters))) != view_digest or read_graph_state(tx, principal.tenant_id)[0] != pin:
            raise GraphViewChanged()
        chunk_ids = frozenset(row["citation"]["chunk_id"] for row in (*mentions, *assertions))
        limits = EvidenceSubgraphLimits(max_entities=4, max_assertions=1, max_paths=1, max_chunk_chars=50_000,
                                       max_total_evidence_chars=500_000)
        nodes: dict[str, dict[str, Any]] = {}
        evidence: dict[str, dict[str, Any]] = {}
        assertion_values: dict[str, Any] = {}
        seed_rows = {(row["entity"]["entity_id"], row["citation"]["chunk_id"]): row for row in mentions}

        def merge(graph: Any, rows: tuple[dict[str, Any], ...]) -> None:
            row_by_id = {str(row.get("mention", row.get("assertion"))["revision_id"]): row for row in rows}
            for node in graph.entities:
                identity = node.entity
                projected = {"entity_id": identity.entity_id, "entity_type": identity.entity_type, "label": identity.canonical_name,
                    "canonical_key": identity.canonical_key, "authority_levels": set(), "mention_revision_ids": set()}
                current = nodes.setdefault(identity.entity_id, projected)
                if any(current[key] != projected[key] for key in ("entity_type", "label", "canonical_key")):
                    raise GraphViewChanged()
                for item in node.evidence:
                    current["authority_levels"].add(item.provenance.authority.value)
                    current["mention_revision_ids"].add(item.provenance.revision_id)
                    row = row_by_id.get(item.provenance.revision_id)
                    if row is not None:
                        evidence[item.provenance.revision_id] = cls._evidence_item(item, row, "ENTITY_MENTION")
            for assertion in graph.assertions:
                assertion_values[assertion.revision_id] = assertion
                evidence[assertion.revision_id] = cls._evidence_item(assertion.evidence, row_by_id[assertion.revision_id], "ASSERTION")

        # Validate each record through the existing strict projection machinery.
        # Per-entity *display* evidence caps must not truncate the authorized
        # view's revision identity set or break pagination stability.
        for row in mentions:
            graph = Neo4jEvidenceSubgraphProjector._project_rows(principal, chunk_ids, (), (row,), limits, policy, version_filter)
            if len(graph.entities) != 1:
                raise GraphViewChanged()
            merge(graph, (row,))
        for row in assertions:
            seed = seed_rows.get((row["seed_entity_id"], row["seed_chunk_id"]))
            if seed is None:
                raise GraphViewChanged()
            graph = Neo4jEvidenceSubgraphProjector._project_rows(principal, chunk_ids, (row,), (seed,), limits, policy, version_filter)
            if len(graph.assertions) != 1:
                raise GraphViewChanged()
            merge(graph, (seed, row))
        if len(evidence) != len(mentions) + len(assertions):
            raise GraphViewChanged()
        for node in nodes.values():
            node["authority_levels"] = tuple(sorted(node["authority_levels"]))
            node["mention_revision_ids"] = tuple(sorted(node["mention_revision_ids"]))
        schema = {"entity_types": tuple(item.to_mapping() for item in tbox.entity_types),
                  "relationship_types": tuple(item.to_mapping() for item in tbox.relationship_types),
                  "hierarchies": tuple(item.to_mapping() for item in tbox.hierarchies)}
        return _View(pin, schema, nodes, assertion_values, evidence, view_digest)

    @staticmethod
    def _evidence_item(item: Any, row: dict[str, Any], kind: str) -> dict[str, Any]:
        record = row["mention" if kind == "ENTITY_MENTION" else "assertion"]
        source_kind = row.get("source_kind")
        applicability: dict[str, Any] = {"family": None, "asset_keys": (), "is_synthetic": None,
            "project_curated_not_company_approved": None, "sme_review_state": "NOT_RECORDED"}
        encoded = row.get("source_metadata")
        if encoded is not None:
            if not isinstance(encoded, str) or len(encoded) > 500_000 or content_checksum(encoded) != row.get("source_metadata_checksum"):
                raise GraphViewChanged()
            metadata = json.loads(encoded)
            citation = item.citation
            if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in (
                ("tenant_id", citation.tenant_id), ("document_id", citation.document_id), ("version_id", citation.version_id), ("source_kind", source_kind),
            )):
                raise GraphViewChanged()
            for key in ("family", "asset_keys", "is_synthetic", "project_curated_not_company_approved"):
                if key in metadata:
                    applicability[key] = tuple(metadata[key]) if key == "asset_keys" else metadata[key]
        elif source_kind is not None:
            raise GraphViewChanged()
        citation = asdict(item.citation)
        citation.pop("tenant_id")
        provenance = asdict(item.provenance)
        for key in ("origin", "authority", "status"):
            provenance[key] = provenance[key].value
        return {"revision_id": item.provenance.revision_id, "record_id": item.provenance.record_id, "record_kind": kind,
            "authority_level": item.provenance.authority.value, "origin": item.provenance.origin.value,
            "status": item.provenance.status.value, "confidence": item.provenance.confidence,
            "reviewed_by": record.get("reviewed_by"), "reviewed_at": record["reviewed_at"].to_native() if hasattr(record.get("reviewed_at"), "to_native") else record.get("reviewed_at"),
            "review_notes": record.get("review_notes"), "source_kind": source_kind, "applicability": applicability,
            "evidence": {"citation": citation, "char_start": item.char_start, "char_end": item.char_end,
                         "quoted_text": item.quoted_text, "provenance": provenance}}

    @staticmethod
    def _selection(view: _View, query: GraphBrowseQuery) -> list[tuple[tuple[int, int, str], str, str]]:
        eligible = {key for key, node in view.nodes.items() if (not query.entity_types or node["entity_type"] in query.entity_types)
                    and (query.name_query is None or query.name_query.casefold() in (node["label"] + " " + node["canonical_key"]).casefold())}
        if query.seed_entity_ids:
            if not set(query.seed_entity_ids) <= eligible:
                return []
            distances = {key: 0 for key in query.seed_entity_ids}
        else:
            distances = {key: 0 for key in eligible}
        allowed = {key: value for key, value in view.assertions.items() if not query.predicates or value.predicate in query.predicates}
        selected: dict[str, int] = {}
        for depth in range(query.hops if query.seed_entity_ids else 1):
            frontier = {key for key, distance in distances.items() if distance == depth}
            for key, value in allowed.items():
                subject, target = value.subject_entity_id, None if value.object_entity_id is None else value.object_entity_id
                outgoing = subject in frontier and query.direction in {"both", "outgoing"}
                incoming = target in frontier and query.direction in {"both", "incoming"}
                if outgoing or incoming:
                    selected.setdefault(key, depth)
                    if target is not None:
                        distances.setdefault(subject, depth + 1)
                        distances.setdefault(target, depth + 1)
        entries = [((depth, 0, key), "assertion", key) for key, depth in selected.items()]
        if not query.predicates:
            entries.extend(((distance, 1, key), "node", key) for key, distance in distances.items())
        elif query.seed_entity_ids:
            entries.extend(((0, 1, key), "node", key) for key in query.seed_entity_ids)
        return sorted(entries)

    def query(self, principal: Principal, query: GraphBrowseQuery, *, version_filter: VersionFilter = VersionFilter(),
              view_token: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        self._authorize(principal)
        if not isinstance(query, GraphBrowseQuery) or not isinstance(version_filter, VersionFilter) or (cursor is not None and view_token is None):
            raise ValueError("invalid graph query")
        previous, expires = (None, None) if view_token is None else self.tokens.decode(principal, "view", view_token)
        view = self._load(principal, query.trust_policy, version_filter)
        claims = {"pin": asdict(view.pin), "version_filter": _filter_payload(version_filter), "trust_policy": query.trust_policy, "view_digest": view.view_digest}
        if previous is not None and previous != claims:
            raise GraphViewChanged()
        token = view_token or self.tokens.encode(principal, "view", claims)
        if expires is None:
            _, expires = self.tokens.decode(principal, "view", token)
        query_digest = digest(asdict(query))
        after: tuple[int, int, str] | None = None
        if cursor is not None:
            continuation, _ = self.tokens.decode(principal, "cursor", cursor)
            if set(continuation) != {"view", "query", "after"} or continuation["view"] != digest(claims) or continuation["query"] != query_digest:
                raise GraphViewChanged()
            after = tuple(continuation["after"])
        entries = [item for item in self._selection(view, query) if after is None or item[0] > after]
        nodes: set[str] = set()
        edges, literals = [], []
        consumed = 0
        last = None
        for ordering, kind, key in entries:
            if consumed >= query.page_size:
                break
            value = view.assertions[key] if kind == "assertion" else None
            endpoints = {key} if value is None else {value.subject_entity_id} | (set() if value.object_entity_id is None else {value.object_entity_id})
            if len(nodes | endpoints) > MAX_GRAPH_NODES or (value is not None and value.object_entity_id is not None and len(edges) >= MAX_GRAPH_EDGES):
                break
            nodes.update(endpoints)
            if value is not None:
                common = {"revision_id": key, "record_id": value.record_id, "predicate": value.predicate,
                    "authority_level": value.evidence.provenance.authority.value, "origin": value.evidence.provenance.origin.value,
                    "confidence": value.evidence.provenance.confidence, "source_kind": view.evidence[key]["source_kind"]}
                if value.object_entity_id is not None:
                    edges.append({**common, "source": value.subject_entity_id, "target": value.object_entity_id})
                else:
                    semantics = None if value.literal_semantics is None else asdict(value.literal_semantics)
                    literals.append({**common, "subject": value.subject_entity_id, "value": value.literal_value, "semantics": semantics})
            consumed += 1
            last = ordering
        has_more = consumed < len(entries)
        next_cursor = None if not has_more else self.tokens.encode(principal, "cursor", {"view": digest(claims), "query": query_digest, "after": last}, expires_at=expires)
        return {"view_token": token, "pin": asdict(view.pin), "schema": view.schema,
            "nodes": tuple(view.nodes[key] for key in sorted(nodes)), "edges": tuple(edges), "literals": tuple(literals),
            "page": {"returned_nodes": len(nodes), "returned_edges": len(edges), "returned_literals": len(literals), "has_more": has_more, "next_cursor": next_cursor},
            "visibility": "AUTHORIZED_SOURCE_VIEW"}

    def evidence(self, principal: Principal, revision_ids: tuple[str, ...], *, view_token: str) -> dict[str, Any]:
        self._authorize(principal)
        if not isinstance(revision_ids, tuple) or not 1 <= len(revision_ids) <= 10 or len(set(revision_ids)) != len(revision_ids) or any(not isinstance(key, str) or not key or len(key) > 256 for key in revision_ids):
            raise ValueError("graph evidence requires 1–10 unique revision IDs")
        claims, _ = self.tokens.decode(principal, "view", view_token)
        try:
            version_filter = _decode_filter(claims["version_filter"])
            view = self._load(principal, claims["trust_policy"], version_filter)
        except (KeyError, TypeError, ValueError) as error:
            raise GraphViewChanged() from error
        current = {"pin": asdict(view.pin), "version_filter": _filter_payload(version_filter), "trust_policy": claims["trust_policy"], "view_digest": view.view_digest}
        if current != claims:
            raise GraphViewChanged()
        items = tuple(view.evidence[key] for key in sorted(revision_ids) if key in view.evidence)
        if sum(len(item["evidence"]["citation"]["chunk_text"]) + len(item["evidence"]["quoted_text"]) for item in items) > 500_000:
            raise GraphBrowseLimitExceeded()
        return {"view_token": view_token, "pin": asdict(view.pin), "items": items}
