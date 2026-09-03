"""Real Neo4j tests for versioned property-graph T-Box persistence."""

from __future__ import annotations

import copy
import ipaddress
import os
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ontology import (
    Neo4jTBoxStore,
    TBoxConflict,
    TBoxStatus,
    TBoxVersion,
)


def _mapping(
    *,
    tenant_id: str = "tenant-industrial",
    version: int = 1,
) -> dict:
    return {
        "tenant_id": tenant_id,
        "key": "rotating-equipment",
        "version": version,
        "status": "DRAFT",
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
                "properties": [],
            }
        ],
    }


def _tbox(*, tenant_id: str = "tenant-industrial", version: int = 1) -> TBoxVersion:
    return TBoxVersion.from_mapping(_mapping(tenant_id=tenant_id, version=version))


class Neo4jTBoxStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (
            "TEST_NEO4J_URI",
            "TEST_NEO4J_USER",
            "TEST_NEO4J_PASSWORD",
            "TEST_NEO4J_DATABASE",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")

        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("integration tests only accept a loopback Neo4j URI")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(
                os.environ["TEST_NEO4J_USER"],
                os.environ["TEST_NEO4J_PASSWORD"],
            ),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        schema_errors = verify_schema(cls.driver, cls.database)
        if schema_errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {schema_errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.store = Neo4jTBoxStore(self.driver, self.database)

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def test_schema_is_structurally_valid_and_online(self) -> None:
        self.assertEqual(verify_schema(self.driver, self.database), [])

    def test_import_round_trips_complete_property_graph_idempotently(self) -> None:
        value = _tbox()
        first = self.store.import_version(value)
        replay = self.store.import_version(value)

        self.assertEqual(first, value)
        self.assertEqual(replay, value)
        self.assertEqual(self.store.get(value.tenant_id, value.tbox_id), value)
        self.assertEqual(self.store.list(value.tenant_id), (value,))
        records, _, _ = self.driver.execute_query(
            """
            MATCH (version:TBoxVersion {tbox_id: $tbox_id})
            OPTIONAL MATCH (version)-[:DECLARES_ENTITY_TYPE]->(entity_type)
            OPTIONAL MATCH (version)-[:DECLARES_RELATIONSHIP_TYPE]->(relationship_type)
            OPTIONAL MATCH (entity_type)-[:DECLARES_PROPERTY]->(entity_property)
            OPTIONAL MATCH (relationship_type)-[:ALLOWED_SOURCE_TYPE]->(source)
            OPTIONAL MATCH (relationship_type)-[:ALLOWED_TARGET_TYPE]->(target)
            RETURN count(DISTINCT entity_type) AS entity_types,
                   count(DISTINCT relationship_type) AS relationship_types,
                   count(DISTINCT entity_property) AS entity_properties,
                   count(DISTINCT source) AS sources,
                   count(DISTINCT target) AS targets
            """,
            tbox_id=value.tbox_id,
            database_=self.database,
        )
        self.assertEqual(dict(records[0]), {
            "entity_types": 2,
            "relationship_types": 1,
            "entity_properties": 2,
            "sources": 1,
            "targets": 1,
        })

    def test_draft_update_requires_checksum_cas_and_published_is_immutable(self) -> None:
        original = _tbox()
        self.store.import_version(original)
        changed_mapping = copy.deepcopy(_mapping())
        changed_mapping["entity_types"][0]["properties"][1]["unit"] = "bar"
        changed = TBoxVersion.from_mapping(changed_mapping)

        with self.assertRaisesRegex(TBoxConflict, "checksum CAS"):
            self.store.import_version(changed)
        self.assertEqual(self.store.get(original.tenant_id, original.tbox_id), original)

        updated = self.store.import_version(
            changed,
            expected_checksum=original.checksum,
        )
        self.assertEqual(updated, changed)
        self.store.publish(
            changed.tenant_id,
            changed.tbox_id,
            expected_active_tbox_id=None,
        )

        second_change_mapping = copy.deepcopy(changed_mapping)
        second_change_mapping["description"] = "illegal published mutation"
        second_change = TBoxVersion.from_mapping(second_change_mapping)
        with self.assertRaisesRegex(TBoxConflict, "immutable"):
            self.store.import_version(
                second_change,
                expected_checksum=changed.checksum,
            )
        self.assertEqual(
            self.store.get(changed.tenant_id, changed.tbox_id).checksum,
            changed.checksum,
        )

    def test_publish_uses_active_pointer_cas_and_retires_previous_version(self) -> None:
        first = _tbox(version=1)
        second = _tbox(version=2)
        self.store.import_version(first)
        self.store.import_version(second)

        published_first = self.store.publish(
            first.tenant_id,
            first.tbox_id,
            expected_active_tbox_id=None,
        )
        self.assertEqual(published_first.status, TBoxStatus.PUBLISHED)
        # An exact retry is safe even when the first response was lost.
        self.assertEqual(
            self.store.publish(
                first.tenant_id,
                first.tbox_id,
                expected_active_tbox_id=None,
            ),
            published_first,
        )

        with self.assertRaisesRegex(TBoxConflict, "CAS failed"):
            self.store.publish(
                second.tenant_id,
                second.tbox_id,
                expected_active_tbox_id=None,
            )
        self.assertEqual(self.store.active(first.tenant_id, first.key), published_first)

        published_second = self.store.publish(
            second.tenant_id,
            second.tbox_id,
            expected_active_tbox_id=first.tbox_id,
        )
        self.assertEqual(published_second.status, TBoxStatus.PUBLISHED)
        self.assertEqual(self.store.active(second.tenant_id, second.key), published_second)
        self.assertEqual(
            self.store.get(first.tenant_id, first.tbox_id).status,
            TBoxStatus.RETIRED,
        )
        self.assertEqual(
            [item.status for item in self.store.list(first.tenant_id)],
            [TBoxStatus.RETIRED, TBoxStatus.PUBLISHED],
        )

    def test_tenant_isolation_applies_to_get_list_active_and_publish(self) -> None:
        first = _tbox(tenant_id="tenant-a")
        other = _tbox(tenant_id="tenant-b")
        self.store.import_version(first)
        self.store.import_version(other)
        self.store.publish(
            first.tenant_id,
            first.tbox_id,
            expected_active_tbox_id=None,
        )

        self.assertEqual(self.store.list("tenant-a"), (
            first.with_status(TBoxStatus.PUBLISHED),
        ))
        self.assertEqual(self.store.list("tenant-b"), (other,))
        self.assertIsNone(self.store.active("tenant-b", other.key))
        with self.assertRaisesRegex(KeyError, "unknown T-Box version"):
            self.store.get("tenant-b", first.tbox_id)
        with self.assertRaisesRegex(KeyError, "unknown T-Box version"):
            self.store.publish(
                "tenant-b",
                first.tbox_id,
                expected_active_tbox_id=None,
            )

    def test_component_identity_constraint_rejects_duplicate_type_name(self) -> None:
        value = _tbox()
        self.store.import_version(value)
        with self.assertRaises(neo4j.exceptions.ConstraintError):
            self.driver.execute_query(
                """
                CREATE (:TBoxEntityType {
                    entity_type_id: 'different-id',
                    tbox_id: $tbox_id,
                    tenant_id: $tenant_id,
                    name: 'Equipment'
                })
                """,
                tbox_id=value.tbox_id,
                tenant_id=value.tenant_id,
                database_=self.database,
            )


if __name__ == "__main__":
    unittest.main()
