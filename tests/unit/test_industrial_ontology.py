"""Explicit taxonomy/composition semantics and publication hierarchy boundaries."""

from __future__ import annotations

from dataclasses import replace
import json
import unittest

from graphrag_prod.industrial.ontology import build_industrial_tbox
from graphrag_prod.knowledge.review import KnowledgePublicationConflict, Neo4jKnowledgePublicationService
from graphrag_prod.ontology import HierarchyDefinition, HierarchyKind, TBoxStatus, TBoxVersion
from graphrag_prod.ontology.hierarchy import HierarchyEdge, HierarchyValidationError, validate_hierarchy_edges
from graphrag_prod.ontology.store import _version_record
from tests.unit.test_ontology import tbox_mapping


def edge(child: str, parent: str, *, predicate: str = "SUBTYPE_OF", child_type: str = "EquipmentClass", parent_type: str = "EquipmentClass") -> HierarchyEdge:
    return HierarchyEdge(child, child_type, predicate, parent, parent_type)


class IndustrialOntologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tbox = build_industrial_tbox("tenant-industrial")

    def test_legacy_checksum_and_mapping_stay_identical_when_hierarchies_absent(self) -> None:
        legacy = TBoxVersion.from_mapping(tbox_mapping())
        self.assertEqual(legacy.checksum, "343c154893a156467b36190f6bc29315890a7b5e0f0c1ab8bc2d342bc5d38714")
        self.assertNotIn("hierarchies", legacy.to_mapping())
        self.assertEqual(TBoxVersion.from_mapping({**tbox_mapping(), "hierarchies": []}).checksum, legacy.checksum)

    def test_composed_schema_distinguishes_class_family_model_asset_and_source_edition(self) -> None:
        self.assertEqual(self.tbox.key, "industrial-electric-v1")
        self.assertEqual(self.tbox.status, TBoxStatus.DRAFT)
        names = {item.name for item in self.tbox.entity_types}
        self.assertTrue({"EquipmentClass", "ProductFamily", "ProductModel", "InstalledAsset", "Component", "SourceEdition"} <= names)
        relationships = {item.name: item for item in self.tbox.relationship_types}
        self.assertEqual(relationships["INSTANCE_OF"].source_types, ("InstalledAsset",))
        self.assertEqual(relationships["INSTANCE_OF"].target_types, ("ProductModel",))
        self.assertEqual(relationships["IN_FAMILY"].target_types, ("ProductFamily",))
        self.assertEqual(relationships["MAY_INDICATE"].target_types, ("FaultMode", "DiagnosticCondition"))
        self.assertIn("DiagnosticCondition", relationships["CHECKED_BY"].source_types)
        self.assertIn("DiagnosticCondition", relationships["ADDRESSED_BY"].source_types)
        self.assertEqual({item.relationship_type for item in self.tbox.hierarchies}, {"SUBTYPE_OF", "PART_OF"})
        self.assertNotIn("CONNECTS_TO", {item.relationship_type for item in self.tbox.hierarchies})
        self.assertFalse(any(prop.name == "layer" for item in self.tbox.entity_types for prop in item.properties))

    def test_hierarchy_round_trip_checksum_order_and_no_implicit_inheritance(self) -> None:
        replay = TBoxVersion.from_json(json.dumps(self.tbox.to_mapping(include_computed=True)))
        self.assertEqual(replay, self.tbox)
        reordered = replace(self.tbox, hierarchies=tuple(
            replace(item, node_types=tuple(reversed(item.node_types)))
            for item in reversed(self.tbox.hierarchies)
        ))
        self.assertEqual(reordered.checksum, self.tbox.checksum)
        self.assertNotEqual(replace(self.tbox, hierarchies=()).checksum, self.tbox.checksum)
        policy = self.tbox.compile_governance_policy()
        self.assertEqual(len(policy.entity_rules), len(self.tbox.entity_types))

    def test_invalid_hierarchy_declarations_are_rejected(self) -> None:
        declaration = self.tbox.hierarchies[0].to_mapping()
        for extra in ({"acyclic": False}, {"acyclic": 1}, {"node_types": []}, {"kind": "INHERITANCE"}, {"layer": 1}, {"node_types": ["EquipmentClass", "InstalledAsset"]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                HierarchyDefinition.from_mapping({**declaration, **extra})
        for declarations in (
            (replace(self.tbox.hierarchies[0], relationship_type="UNKNOWN"),),
            (replace(self.tbox.hierarchies[0], node_types=("UnknownClass",)),),
            (self.tbox.hierarchies[0], self.tbox.hierarchies[0]),
        ):
            with self.subTest(declarations=declarations), self.assertRaises(ValueError):
                replace(self.tbox, hierarchies=declarations)

    def test_dag_roots_depth_and_duplicate_evidence_are_deterministic(self) -> None:
        edges = (edge("vacuum", "breaker"), edge("breaker", "equipment"), edge("vacuum", "equipment"), edge("vacuum", "breaker"))
        summary = validate_hierarchy_edges(self.tbox, edges)
        taxonomy = next(item for item in summary if item.name == "EquipmentClassification")
        self.assertEqual((taxonomy.node_count, taxonomy.edge_count, taxonomy.maximum_depth), (3, 3, 2))
        self.assertEqual(taxonomy.root_ids, ("equipment",))
        self.assertEqual(validate_hierarchy_edges(self.tbox, reversed(edges)), summary)

    def test_cycles_and_self_loops_fail_in_each_hierarchy_independently(self) -> None:
        for edges in (
            (edge("a", "a"),),
            (edge("a", "b"), edge("b", "c"), edge("c", "a")),
            (edge("a", "b", predicate="PART_OF", child_type="Component", parent_type="Component"), edge("b", "a", predicate="PART_OF", child_type="Component", parent_type="Component")),
        ):
            with self.subTest(edges=edges), self.assertRaises(HierarchyValidationError):
                validate_hierarchy_edges(self.tbox, edges)
        # A cyclic electrical connection is not a cyclic composition assertion.
        validate_hierarchy_edges(self.tbox, (
            edge("a", "b", predicate="CONNECTS_TO", child_type="Component", parent_type="Component"),
            edge("b", "a", predicate="CONNECTS_TO", child_type="Component", parent_type="Component"),
        ))

    def test_hierarchy_endpoint_types_and_conflicting_identity_types_fail(self) -> None:
        with self.assertRaisesRegex(HierarchyValidationError, "endpoint"):
            validate_hierarchy_edges(self.tbox, (edge("asset", "class", child_type="InstalledAsset"),))
        with self.assertRaisesRegex(HierarchyValidationError, "identity"):
            validate_hierarchy_edges(self.tbox, (
                edge("same", "asset", predicate="PART_OF", child_type="Component", parent_type="InstalledAsset"),
                edge("same", "system", predicate="PART_OF", child_type="InstalledAsset", parent_type="IndustrialSystem"),
            ))

    def test_deep_hierarchy_is_iterative_and_input_budget_is_enforced(self) -> None:
        result = validate_hierarchy_edges(self.tbox, (edge(str(index), str(index + 1)) for index in range(1_500)))
        self.assertEqual(next(item for item in result if item.name == "EquipmentClassification").maximum_depth, 1_500)
        with self.assertRaisesRegex(HierarchyValidationError, "budget"):
            validate_hierarchy_edges(self.tbox, (edge("a", "b") for _ in range(5_001)))

    def test_publication_boundary_reloads_exact_checksum_bound_tbox(self) -> None:
        record = {**_version_record(self.tbox), "status": "PUBLISHED"}
        class Tx:
            def __init__(self, value):
                self.value = value
            def run(self, query, **parameters):
                self.parameters = parameters
                return ({"tbox": self.value},)
        tx = Tx(record)
        Neo4jKnowledgePublicationService._validate_hierarchies_tx(tx, self.tbox.tenant_id, self.tbox.tbox_id, ())
        self.assertEqual(tx.parameters["tenant_id"], "tenant-industrial")
        for corrupted in ({**record, "checksum": "0" * 64}, {**record, "definition_json": "{}"}):
            with self.subTest(corrupted=corrupted), self.assertRaises(KnowledgePublicationConflict):
                Neo4jKnowledgePublicationService._validate_hierarchies_tx(Tx(corrupted), self.tbox.tenant_id, self.tbox.tbox_id, ())


if __name__ == "__main__":
    unittest.main()
