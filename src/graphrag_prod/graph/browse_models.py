"""Bounded graph browsing contracts, independent of HTTP and screen layout."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


MAX_GRAPH_RECORDS = 500
MAX_GRAPH_NODES = 150
MAX_GRAPH_EDGES = 200
MAX_GRAPH_HOPS = 2
GRAPH_READ_CAPABILITY = "knowledge:graph:read"


class GraphBrowseError(RuntimeError):
    """Sanitized graph read failure."""


class GraphViewChanged(GraphBrowseError):
    def __init__(self) -> None:
        super().__init__("the graph view changed; refresh the graph")


class GraphBrowseLimitExceeded(GraphBrowseError):
    def __init__(self) -> None:
        super().__init__("the graph exceeds a read budget; narrow the source scope")


class GraphBrowseUnavailable(GraphBrowseError):
    def __init__(self) -> None:
        super().__init__("the graph store is temporarily unavailable")


def _integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its bound")


def _text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} requires a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class GraphReadPin:
    publication_id: str | None
    publication_generation: int
    activation_generation: int
    ontology_version_id: str | None
    tbox_checksum: str | None
    corpus_revision: int

    def __post_init__(self) -> None:
        for name in ("publication_generation", "activation_generation", "corpus_revision"):
            _integer(getattr(self, name), name, 0, 2**63 - 1)
        if self.publication_id is None:
            if self.publication_generation != 0 or self.ontology_version_id is not None or self.tbox_checksum is not None:
                raise ValueError("empty publication pin has publication metadata")
        else:
            _text(self.publication_id, "publication_id")
            _text(self.ontology_version_id, "ontology_version_id")
            if self.publication_generation < 1 or self.activation_generation < 1 or not isinstance(self.tbox_checksum, str) or re.fullmatch(r"[0-9a-f]{64}", self.tbox_checksum) is None:
                raise ValueError("published graph pin is incomplete")


@dataclass(frozen=True, slots=True)
class GraphBrowseQuery:
    trust_policy: str = "PUBLISHED_SECONDARY_INCLUSIVE"
    entity_types: tuple[str, ...] = ()
    predicates: tuple[str, ...] = ()
    name_query: str | None = None
    seed_entity_ids: tuple[str, ...] = ()
    direction: str = "both"
    hops: int = 1
    page_size: int = 100

    def __post_init__(self) -> None:
        if self.trust_policy not in {"PUBLISHED_SECONDARY_INCLUSIVE", "AUTHORITATIVE_ONLY"}:
            raise ValueError("unsupported graph trust policy")
        if self.direction not in {"both", "outgoing", "incoming"}:
            raise ValueError("unsupported graph direction")
        _integer(self.hops, "hops", 1, MAX_GRAPH_HOPS)
        _integer(self.page_size, "page_size", 1, MAX_GRAPH_EDGES)
        for name, maximum in (("entity_types", 64), ("predicates", 64), ("seed_entity_ids", 8)):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) > maximum or len(values) != len(set(values)):
                raise ValueError(f"{name} must be a bounded unique tuple")
            for item in values:
                _text(item, name)
                if name != "seed_entity_ids" and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", item) is None:
                    raise ValueError(f"{name} contains an invalid type name")
            object.__setattr__(self, name, tuple(sorted(values)))
        if self.name_query is not None:
            if not isinstance(self.name_query, str) or not self.name_query.strip() or len(self.name_query) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in self.name_query):
                raise ValueError("name_query requires bounded non-empty text")
            object.__setattr__(self, "name_query", self.name_query.strip())


GRAPH_STATE_QUERY = """
// governed-graph:read-pin
OPTIONAL MATCH (corpus:TenantCorpusState {tenant_id:$tenant_id})
OPTIONAL MATCH (state:KnowledgePublicationState {tenant_id:$tenant_id})
OPTIONAL MATCH (state)-[active:ACTIVE_KNOWLEDGE_PUBLICATION]->(publication)
OPTIONAL MATCH (publication)-[binding:USES_TBOX_VERSION]->(tbox)
RETURN coalesce(corpus.corpus_revision,0) AS corpus_revision,
       coalesce(state.activation_generation,0) AS activation_generation,
       publication {.publication_id,.tenant_id,.generation,.ontology_version_id,.status} AS publication,
       count(DISTINCT active) AS active_links,
       count(binding) AS tbox_links,
       collect(tbox {.tbox_id,.tenant_id,.key,.version,.status,.checksum,.definition_json}) AS tboxes
LIMIT 2
"""


def read_graph_state(tx: Any, tenant_id: str) -> tuple[GraphReadPin, Any]:
    """Read a tiny operational boundary; never export publication members."""
    from graphrag_prod.ontology.store import TBoxConflict, _decode_tbox

    try:
        rows = list(tx.run(GRAPH_STATE_QUERY, tenant_id=tenant_id))
        if len(rows) != 1:
            raise GraphViewChanged()
        row = rows[0]
        publication = row.get("publication")
        if publication is None:
            if row["active_links"] != 0 or row["tbox_links"] != 0:
                raise GraphViewChanged()
            return GraphReadPin(None, 0, row["activation_generation"], None, None, row["corpus_revision"]), None
        if row["active_links"] != 1 or row["tbox_links"] != 1 or len(row["tboxes"]) != 1 or publication["tenant_id"] != tenant_id or publication["status"] != "ACTIVE":
            raise GraphViewChanged()
        tbox = _decode_tbox(row["tboxes"][0])
        if tbox.tenant_id != tenant_id or tbox.status.value not in {"PUBLISHED", "RETIRED"} or tbox.tbox_id != publication["ontology_version_id"]:
            raise GraphViewChanged()
        return GraphReadPin(publication["publication_id"], publication["generation"], row["activation_generation"], tbox.tbox_id, tbox.checksum, row["corpus_revision"]), tbox
    except GraphBrowseError:
        raise
    except (KeyError, TypeError, ValueError, TBoxConflict) as error:
        raise GraphViewChanged() from error
