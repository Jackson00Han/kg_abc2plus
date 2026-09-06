"""Cross-artifact checks for the industrial domain, without provider calls."""

from __future__ import annotations

from typing import Any

from graphrag_prod.ontology import TBoxVersion
from graphrag_prod.ontology.hierarchy import HierarchyEdge, validate_hierarchy_edges

from .contract import validate_industrial_contract
from .corpus import (
    FAMILY_TO_CONTRACT, IndustrialCorpus, compute_corpus_checksum, corpus_report,
    validate_corpus,
)
from .sources import SourceCatalog


# Reviewed applicability bindings. Similar titles or a shared manufacturer are
# deliberately insufficient to add another source to this core model.
CORE_SOURCE_FAMILIES = {
    "canalis-kt-installation": "canalis-kt",
    "canalis-kt-run-components": "canalis-kt",
    "evopact-hvx-up-to-24kv": "evopact-hvx-up24",
}


def validate_industrial_model(
    corpus: IndustrialCorpus,
    catalog: SourceCatalog,
    contract: dict[str, Any],
    tbox: TBoxVersion,
) -> dict[str, Any]:
    """Check identity, source applicability and graph semantics together.

    This verifies the application data contract. It does not establish that an
    expert has approved the technical meaning of every authored sentence.
    """
    validate_industrial_contract(contract)
    validate_corpus(corpus)
    if corpus.tenant_id != tbox.tenant_id:
        raise ValueError("corpus and ontology must belong to the same tenant")
    hierarchy_bindings = {item.relationship_type: (item.kind.value, frozenset(item.node_types))
                          for item in tbox.hierarchies}
    required_hierarchies = {
        "SUBTYPE_OF": ("CLASSIFICATION", frozenset({"EquipmentClass"})),
        "PART_OF": ("COMPOSITION", frozenset({"IndustrialSystem", "InstalledAsset", "Component"})),
    }
    if any(hierarchy_bindings.get(key) != value for key, value in required_hierarchies.items()):
        raise ValueError("industrial classification and composition hierarchy contracts are required")
    if set(FAMILY_TO_CONTRACT.values()) != set(contract["scope"]["core_families"]):
        raise ValueError("corpus families differ from the industrial contract")
    limits = contract["corpus"]
    if len(corpus.documents) < limits["minimum_authored_documents"]:
        raise ValueError("authored documents do not satisfy the industrial contract")
    if not limits["minimum_chunks"] <= corpus.chunk_count <= limits["maximum_chunks"]:
        raise ValueError("source Chunks do not satisfy the industrial contract")
    if sum(item.type_name == "InstalledAsset" for item in corpus.entities) < limits["minimum_installed_assets"]:
        raise ValueError("installed assets do not satisfy the industrial contract")
    for document in corpus.documents:
        for reference in document.source_refs:
            source = catalog.get(reference.source_id)
            if (
                CORE_SOURCE_FAMILIES.get(source.source_id) != document.family
                or source.applicability != "CORE"
                or source.extraction_status != "TEXT_READY"
                or source.sha256 is None
            ):
                raise ValueError("source reference is outside the reviewed product family")
            if reference.embedded_revision != source.embedded_revision:
                raise ValueError("source reference uses a different embedded edition")
            if source.physical_pages is None or max(reference.physical_pages) > source.physical_pages:
                raise ValueError("source reference points outside the physical document")
    types = {item.name: item for item in tbox.entity_types}
    relationships = {item.name: item for item in tbox.relationship_types}
    entities = {item.key: item for item in corpus.entities}
    for entity in corpus.entities:
        definition = types.get(entity.type_name)
        if definition is None or entity.identity.split(":", 1)[0] not in definition.canonical_key_namespaces:
            raise ValueError("corpus entity is outside the ontology identity contract")
    edges = []
    outgoing: dict[tuple[str, str], set[str]] = {}
    incoming: dict[tuple[str, str], set[str]] = {}
    for relation in corpus.relationships:
        definition = relationships.get(relation.predicate)
        subject, target = entities[relation.subject_key], entities[relation.object_key]
        if (
            definition is None or subject.type_name not in definition.source_types
            or target.type_name not in definition.target_types
        ):
            raise ValueError("corpus relation violates the ontology direction or endpoint types")
        edges.append(HierarchyEdge(
            subject.identity, subject.type_name, relation.predicate,
            target.identity, target.type_name,
        ))
        outgoing.setdefault((relation.predicate, subject.key), set()).add(target.key)
        incoming.setdefault((relation.predicate, target.key), set()).add(subject.key)
    # The selected corpus graph is a complete proposed manifest, so closed-world
    # endpoint checks are meaningful here, before expensive source ingestion.
    for definition in tbox.relationship_types:
        for entity in corpus.entities:
            for allowed_types, cardinality, neighbors in (
                (definition.source_types, definition.source_cardinality, outgoing),
                (definition.target_types, definition.target_cardinality, incoming),
            ):
                if entity.type_name not in allowed_types:
                    continue
                count = len(neighbors.get((definition.name, entity.key), set()))
                if (cardinality.required and count == 0) or (cardinality.single_valued and count > 1):
                    raise ValueError("corpus relation violates a declared endpoint cardinality")
    hierarchies = validate_hierarchy_edges(tbox, edges)
    report = corpus_report(corpus)
    if report["counts"]["record_upper_bound"] > limits["maximum_published_records"]:
        raise ValueError("selected graph exceeds the declared publication limit")
    if corpus.manifest_checksum != compute_corpus_checksum(corpus):
        raise ValueError("corpus manifest checksum differs from its source and graph content")
    return {
        "schema_version": "industrial-model-validation-v1",
        "profile": contract["profile"], "production_candidate_eligible": False,
        "corpus_checksum": corpus.manifest_checksum, "tbox_checksum": tbox.checksum,
        "source_catalog_version": catalog.version,
        "counts": report["counts"],
        "hierarchies": [
            {"name": item.name, "nodes": item.node_count, "edges": item.edge_count,
             "roots": len(item.root_ids), "maximum_depth": item.maximum_depth}
            for item in hierarchies
        ],
        "technical_sme_review": "PENDING",
    }
