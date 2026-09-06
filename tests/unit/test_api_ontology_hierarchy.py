"""Hierarchy transport reaches the canonical tenant-owned T-Box unchanged."""

from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import (
    OntologyImportRequest,
    OntologyListRequest,
)
from graphrag_prod.api.runtime import RequestValidationError
from graphrag_prod.domain import Principal


def hierarchy_payload() -> dict:
    return {
        "key": "industrial-hierarchy-test", "version": 1,
        "entity_types": [{"name": "EquipmentClass", "canonical_key_namespaces": ["class"]}],
        "relationship_types": [{
            "name": "SUBTYPE_OF", "source_types": ["EquipmentClass"],
            "target_types": ["EquipmentClass"],
        }],
        "hierarchies": [{
            "name": "classification", "relationship_type": "SUBTYPE_OF",
            "kind": "CLASSIFICATION", "node_types": ["EquipmentClass"],
            "acyclic": True,
        }],
    }


class MemoryTBoxes:
    def __init__(self) -> None:
        self.values = []

    def import_version(self, value, *, expected_checksum=None):
        self.values.append(value)
        return value

    def list(self, tenant_id, **filters):
        return tuple(value for value in self.values if value.tenant_id == tenant_id)


class OntologyHierarchyAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryTBoxes()
        self.operations = Neo4jKnowledgeOperations(
            driver=object(), construction=SimpleNamespace(run=lambda: None), tboxes=self.store,
        )
        self.principal = Principal(
            "industrial-curator", "industrial-tenant", frozenset({"engineering"}),
            frozenset({"ontology:write", "ontology:read"}),
        )

    def test_import_list_preserve_hierarchy_and_verified_tenant(self) -> None:
        imported = self.operations.ontology_import(
            self.principal, OntologyImportRequest.model_validate(hierarchy_payload()),
        ).payload
        listed = self.operations.ontology_list(self.principal, OntologyListRequest()).payload
        self.assertEqual(self.store.values[0].tenant_id, "industrial-tenant")
        self.assertEqual(imported.hierarchies[0].relationship_type, "SUBTYPE_OF")
        self.assertEqual(imported.hierarchies, listed.items[0].hierarchies)
        self.assertEqual(imported.checksum, self.store.values[0].checksum)

    def test_legacy_transport_keeps_definition_without_hierarchy(self) -> None:
        payload = hierarchy_payload()
        payload.pop("hierarchies")
        imported = self.operations.ontology_import(
            self.principal, OntologyImportRequest.model_validate(payload),
        ).payload
        self.assertEqual(imported.hierarchies, ())
        self.assertNotIn("hierarchies", self.store.values[0].canonical_definition)

    def test_unknown_hierarchy_relation_is_rejected_before_persistence(self) -> None:
        payload = hierarchy_payload()
        payload["hierarchies"][0]["relationship_type"] = "UNDECLARED"
        with self.assertRaises(RequestValidationError):
            self.operations.ontology_import(
                self.principal, OntologyImportRequest.model_validate(payload),
            )
        self.assertEqual(self.store.values, [])

    def test_bounds_identity_injection_and_weakened_semantics_fail_closed(self) -> None:
        variants = []
        for change in ({"acyclic": False}, {"acyclic": 1}, {"kind": "LAYOUT"},
                       {"node_types": []}, {"node_types": ["EquipmentClass"] * 65},
                       {"tenant_id": "another-tenant"}):
            value = hierarchy_payload()
            value["hierarchies"][0].update(change)
            variants.append(value)
        value = hierarchy_payload()
        value["hierarchies"] *= 33
        variants.append(value)
        value = hierarchy_payload()
        duplicate = copy.deepcopy(value["hierarchies"][0])
        duplicate["name"] = "second_classification"
        value["hierarchies"].append(duplicate)
        variants.append(value)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                OntologyImportRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
