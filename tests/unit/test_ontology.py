"""Property-graph T-Box model and validation tests."""

from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from graphrag_prod.ontology import (
    Cardinality,
    EntityTypeDefinition,
    Neo4jTBoxStore,
    PropertyDataType,
    PropertyDefinition,
    TBoxConflict,
    TBoxStatus,
    TBoxValidationError,
    TBoxVersion,
)


def tbox_mapping(
    *,
    tenant_id: str = "tenant-industrial",
    version: int = 1,
) -> dict:
    return {
        "tenant_id": tenant_id,
        "key": "rotating-equipment",
        "version": version,
        "status": "DRAFT",
        "description": "Governed rotating equipment schema",
        "entity_types": [
            {
                "name": "Equipment",
                "canonical_key_namespaces": ["asset"],
                "identity_properties": ["serial_number"],
                "properties": [
                    {
                        "name": "serial_number",
                        "datatype": "STRING",
                        "required": True,
                        "cardinality": "ONE",
                    },
                    {
                        "name": "design_pressure",
                        "datatype": "DECIMAL",
                        "required": False,
                        "cardinality": "ZERO_OR_ONE",
                        "unit": "kPa",
                    },
                ],
            },
            {
                "name": "Plant",
                "canonical_key_namespaces": ["plant"],
                "properties": [],
            },
        ],
        "relationship_types": [
            {
                "name": "INSTALLED_AT",
                "source_types": ["Equipment"],
                "target_types": ["Plant"],
                "source_cardinality": "ZERO_OR_ONE",
                "target_cardinality": "ZERO_OR_MORE",
                "properties": [
                    {
                        "name": "installed_on",
                        "datatype": "DATE",
                        "required": False,
                        "cardinality": "ZERO_OR_ONE",
                    }
                ],
            }
        ],
    }


class TBoxModelTests(unittest.TestCase):
    def test_strict_json_round_trip_computes_stable_identity_and_checksum(self) -> None:
        value = TBoxVersion.from_mapping(tbox_mapping())
        replay = TBoxVersion.from_json(
            json.dumps(value.to_mapping(include_computed=True))
        )

        self.assertEqual(value, replay)
        self.assertEqual(value.tbox_id, replay.tbox_id)
        self.assertEqual(len(value.checksum), 64)
        self.assertEqual(value.status, TBoxStatus.DRAFT)
        self.assertEqual(
            value.entity_types[0].properties[1].datatype,
            PropertyDataType.DECIMAL,
        )
        self.assertEqual(
            value.relationship_types[0].source_cardinality,
            Cardinality.ZERO_OR_ONE,
        )

    def test_checksum_is_semantic_and_ignores_order_and_lifecycle_status(self) -> None:
        original = TBoxVersion.from_mapping(tbox_mapping())
        reordered_mapping = tbox_mapping()
        reordered_mapping["entity_types"].reverse()
        reordered_mapping["entity_types"][1]["properties"].reverse()
        reordered = TBoxVersion.from_mapping(reordered_mapping)
        published = original.with_status(TBoxStatus.PUBLISHED)

        self.assertEqual(original.checksum, reordered.checksum)
        self.assertEqual(original.checksum, published.checksum)
        self.assertEqual(original.tbox_id, reordered.tbox_id)
        self.assertNotEqual(
            original.tbox_id,
            TBoxVersion.from_mapping(tbox_mapping(version=2)).tbox_id,
        )
        self.assertNotEqual(
            original.tbox_id,
            TBoxVersion.from_mapping(tbox_mapping(tenant_id="tenant-other")).tbox_id,
        )

    def test_definitions_are_deeply_immutable(self) -> None:
        value = TBoxVersion.from_mapping(tbox_mapping())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.key = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            value.entity_types[0].properties[0].required = False  # type: ignore[misc]

    def test_compile_preserves_existing_governance_contract(self) -> None:
        policy = TBoxVersion.from_mapping(tbox_mapping()).compile_governance_policy(
            minimum_entity_confidence=0.9,
            minimum_assertion_confidence=0.85,
            anomalous_hub_degree=250,
        )
        self.assertTrue(
            policy.allows_relationship(
                "INSTALLED_AT", "Equipment", "entity", "Plant"
            )
        )
        self.assertFalse(
            policy.allows_relationship(
                "INSTALLED_AT", "Plant", "entity", "Equipment"
            )
        )
        self.assertEqual(policy.minimum_entity_confidence, 0.9)
        self.assertEqual(policy.policy_version, 1)

    def test_type_names_and_relationship_endpoints_are_validated(self) -> None:
        duplicate = tbox_mapping()
        duplicate["entity_types"].append(
            {
                "name": "equipment",
                "canonical_key_namespaces": ["other"],
            }
        )
        with self.assertRaisesRegex(ValueError, "entity type names must be unique"):
            TBoxVersion.from_mapping(duplicate)

        unknown_endpoint = tbox_mapping()
        unknown_endpoint["relationship_types"][0]["target_types"] = ["Unknown"]
        with self.assertRaisesRegex(ValueError, "unknown entity types: Unknown"):
            TBoxVersion.from_mapping(unknown_endpoint)

        invalid_name = tbox_mapping()
        invalid_name["entity_types"][0]["name"] = "Rotating Equipment"
        with self.assertRaisesRegex(ValueError, "contain only letters"):
            TBoxVersion.from_mapping(invalid_name)

    def test_property_datatype_unit_required_and_cardinality_are_validated(self) -> None:
        cases = []

        invalid_datatype = tbox_mapping()
        invalid_datatype["entity_types"][0]["properties"][0]["datatype"] = "NUMBER"
        cases.append((invalid_datatype, "property datatype must be one of"))

        invalid_unit = tbox_mapping()
        invalid_unit["entity_types"][0]["properties"][0]["unit"] = "serials"
        cases.append((invalid_unit, "unit is allowed only for numeric"))

        inconsistent_required = tbox_mapping()
        inconsistent_required["entity_types"][0]["properties"][0]["required"] = False
        cases.append((inconsistent_required, "required must agree"))

        invalid_cardinality = tbox_mapping()
        invalid_cardinality["relationship_types"][0]["source_cardinality"] = "MANY"
        cases.append((invalid_cardinality, "source cardinality must be one of"))

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    TBoxVersion.from_mapping(payload)

    def test_tbox_import_rejects_unrecognized_pint_unit_before_database_access(
        self,
    ) -> None:
        class _NoSessionDriver:
            def session(self, **_kwargs: object) -> object:
                raise AssertionError(
                    "invalid declared units must fail before database access"
                )

        value = TBoxVersion(
            tenant_id="tenant-industrial",
            key="invalid-units",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(
                EntityTypeDefinition(
                    "Equipment",
                    ("asset",),
                    properties=(
                        PropertyDefinition(
                            "design_pressure",
                            PropertyDataType.DECIMAL,
                            False,
                            Cardinality.ZERO_OR_ONE,
                            unit="definitely_not_a_pint_unit_xyz",
                        ),
                    ),
                ),
            ),
            relationship_types=(),
        )

        with self.assertRaisesRegex(TBoxValidationError, "unrecognized Pint unit"):
            Neo4jTBoxStore(_NoSessionDriver()).import_version(value)

    def test_property_and_identity_names_are_validated(self) -> None:
        duplicate = tbox_mapping()
        duplicate["entity_types"][0]["properties"].append(
            {
                "name": "SERIAL_NUMBER",
                "datatype": "STRING",
                "required": True,
                "cardinality": "ONE",
            }
        )
        with self.assertRaisesRegex(ValueError, "property names must be unique"):
            TBoxVersion.from_mapping(duplicate)

        undeclared_identity = tbox_mapping()
        undeclared_identity["entity_types"][0]["identity_properties"] = ["tag"]
        with self.assertRaisesRegex(ValueError, "reference declared properties"):
            TBoxVersion.from_mapping(undeclared_identity)

        optional_identity = tbox_mapping()
        optional_identity["entity_types"][0]["identity_properties"] = [
            "design_pressure"
        ]
        with self.assertRaisesRegex(ValueError, "required and single-valued"):
            TBoxVersion.from_mapping(optional_identity)

    def test_mapping_is_strict_and_verifies_computed_metadata(self) -> None:
        unknown = tbox_mapping()
        unknown["rdf_namespace"] = "not-supported"
        with self.assertRaisesRegex(ValueError, "unknown fields: rdf_namespace"):
            TBoxVersion.from_mapping(unknown)

        original = TBoxVersion.from_mapping(tbox_mapping())
        wrong_checksum = original.to_mapping(include_computed=True)
        wrong_checksum["checksum"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum does not match"):
            TBoxVersion.from_mapping(wrong_checksum)

        invalid_json = copy.deepcopy(tbox_mapping())
        invalid_json["version"] = True
        with self.assertRaisesRegex(ValueError, "positive integer"):
            TBoxVersion.from_mapping(invalid_json)


if __name__ == "__main__":
    unittest.main()
